# Coaching behavior cases

Scenario cases for the behavior the Coach is supposed to hold when the evidence
invites it to drift. They exist because prose in
[SKILL.md](../.agents/skills/garmin-coach-loop/SKILL.md) can be satisfied word by
word while the actual answer still slides — a review that reports completion as
progress, or a plan that changes because the athlete asked twice. Tracked in issue #25.

They are also the gate on the instruction layer itself ([AGENTS.md](../AGENTS.md) 11
and 12, issue #82). An instruction earns its place by fixing a case that fails
without it, so a case has to be able to fail: one that scores the answer against a
rule rather than against its own evidence would pass a coach reasoning by rule, which
is the failure it was supposed to catch. Write `expected` and `fails_if` from what
this scenario's evidence supports, never from a general mapping between a completion
state and an adjustment.

## What a case is

One JSON file per case in [`cases/`](cases), named after its `case_id`:

| field | meaning |
| --- | --- |
| `case_id` | Stable identifier, and the file name. |
| `issues` | The issues this case is evidence for. |
| `mode` | The DecisionEvent mode the turn belongs to. |
| `scenario` | What the athlete asks, and when. |
| `given` | The evidence in play, as facts. Nothing here is a verdict. |
| `evidence_fields` | The contract fields the answer has to actually read. Every path is checked against `contracts/` — a renamed field fails the case, not just the code. |
| `expected.conclusion` | The finding a competent review reaches. |
| `expected.must_state` | What the answer has to contain to be useful. |
| `expected.must_not_state` | The claims that make the answer wrong even if it sounds right. |
| `fails_if` | How a too-agreeable or over-reacting coach fails this case. |

## Running one

The product never calls an LLM API ([AGENTS.md](../AGENTS.md)), so these are not
executed by the test suite. Give the case's `scenario` and `given` to the Coach as
a turn, then score its answer against `expected` and `fails_if`. Score the decision
and the explanation separately: a right call for an unstated reason does not pass,
because the next turn will not repeat it.

`python3 -m unittest discover -s tests -p 'test_*.py'` checks only that the cases
are well formed and still name real contract fields.
