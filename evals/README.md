# Coaching evaluation

How this repository evaluates coaching judgment. Prose in
[SKILL.md](../.agents/skills/garmin-coach-loop/SKILL.md) can be satisfied word by word
while the answer still slides — a review that reports completion as progress, a plan
that changes because the athlete asked twice — so the behaviour is held by cases
rather than by the wording that is supposed to produce it.

This does not call an LLM API ([AGENTS.md](../AGENTS.md)), and it does not grade
product mechanics that code and tests already own.

The case format and discipline are in force. [`ab/`](ab) already provides frozen
packets, immutable answers, repeated samples and side-by-side reports. Issue #86
tracks the remaining measurement evidence, not a harness still to be built.

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

An `evidence_fields` path names a CoachContext field unless it opens with one of two
prefixes, which pick the other thing a read hands back: `plan.` for the PlanState beside
the context, and `pre_plan_observations.` for what an account with no plan gets instead
of one — the training the provider already holds, and anything the athlete stated before
there was a plan to hold it. The last is the only evidence a first plan has, so a
`plan_cycle` case about the first plan names its fields there.

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

**Binding proves the fields exist, not that the facts are true.**
`tests/test_coach_session_scenarios.py` checks that every path in `evidence_fields`
resolves against a committed read of the same mode. It cannot check `given`, which is
prose. So a case can bind cleanly, pass every test, and describe a scenario that is not
the one it binds to — asserting a session was completed where the read has it still
planned, or that a condition went unmet where the evidence says it was met. That case
then scores a correct answer as a failure, which is worse than having no case.

The check is manual and belongs to whoever writes the case: open the bound scenario's
snapshot and read the values behind every sentence of `given`. One case here was merged
with a `given` that contradicted its own scenario on two counts, and it took a blind
answering run to find it — the coach read the evidence correctly and the ruler was
wrong.

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

Use the existing [`ab/` run and repeat protocol](ab/README.md#running-one) for
packet-bound answers and comparisons; single-arm suites are supported. It does not
automatically execute or grade all behavior cases. The fallback suite demonstrates
binding specific cases to anonymous scenario reads and recording independent answers.
Freeze the suite, exact served texts and inputs, retain every answer and name its
executor, then score the case manually. Never infer a behavioral pass from a schema
check or the existence of a harness.

The [issue #25 rubric calibration](baselines/issue-25-compliant-calibration.md)
retains deliberately wrong answers and a valid control. These are authored calibration
examples, not sampled model outputs or evidence of a model's failure rate.

`python3 -m unittest discover -s tests -p 'test_*.py'` checks deterministic case binding,
anonymous scenario builds, packet integrity and recording/report mechanics. Stochastic
coaching answers are collected outside unit CI and reviewed separately; neither their
variance nor a live LLM verdict is a hard unit-test gate.

## What does not belong here

The rubric says what a good answer reads like — its register, its ordering, whether
it translated field names into words the athlete can act on. That is grading, and it
stays in the suite. It reaches `SKILL.md` only under [AGENTS.md](../AGENTS.md) 12,
which asks for the concrete run where the coach failed without it. The rubric
describing an expectation is not itself that evidence.
