# Coaching decision evidence

Phase 1 of the historical-training work defines **what the coach needs to know to make a decision** before changing the tool surface or introducing a new persisted history model.

This document is deliberately a decision contract, not an implementation of a new tool. The current ChatGPT Apps / MCP review surface must remain stable while this model is evaluated. Tool changes belong to Phase 2.

## 1. The unit the coach reads

The coach should not receive a database-shaped history. It should receive a finite set of **decision evidence** for the current coaching question.

The evidence has four layers:

1. **Current decision state** — goal, constraints, current plan, current week, and explicit safety signals.
2. **Recent response** — what was prescribed recently, what actually happened, and the athlete's reported response.
3. **Relevant historical patterns** — a small number of prior training episodes that are materially similar to the current decision.
4. **Long-range background** — compact aggregates that establish training history without narrating individual sessions.

The coach does not need to know how the evidence was fetched, reconciled, stored, or compressed. Those are product concerns.

## 2. Historical evidence is not symmetric

Not every session deserves the same historical representation.

### Low-information / repeatable sessions

Examples:

- ordinary easy runs;
- Long 2 / long easy aerobic runs when there is no unusual response;
- routine recovery work;
- ordinary maintenance sessions that neither progress nor fail.

Historical representation should normally collapse these to a compact aggregate or a very small factual record.

Example:

> Long easy run, 80 min, completed as planned, no reported issue.

The coach does not need every lap, every Garmin field, or the original prose prescription months later.

### High-information sessions

Examples:

- interval / threshold / VO2 sessions with explicit structure;
- progressive workouts where the prescription changes over time;
- benchmark or test sessions;
- sessions with a meaningful partial completion;
- sessions repeatedly missed, failed, shortened, or replaced;
- sessions followed by an unusual recovery or athlete-reported response;
- strength movements with an explicit progression or regression.

These retain more structure because the **relationship between prescription and response** can change a future decision.

## 3. The key historical object: training episode

When a session has enough decision value to retain detail, its useful historical representation is a **training episode**, not the raw plan and not the raw activity.

Conceptually:

```text
training episode
├── prescribed intent
│   ├── session type / adaptation
│   ├── progression target, if any
│   └── relevant dose / structure
├── execution
│   ├── completed / partial / missed / moved / replaced
│   ├── concise actual summary
│   └── execution confidence / provenance
├── athlete response
│   ├── explicit subjective feedback
│   └── relevant recovery response
└── consequence
    └── what subsequently happened that is useful evidence
```

This is intentionally descriptive. It must not become a deterministic `session_score`, readiness score, or automatic causal label.

For example, the useful historical fact is:

> 6×3 min interval session was prescribed; 4 reps were completed; the athlete reported that the last two were unsustainable; the following quality session was reduced.

It is **not**:

> interval tolerance = 67%

The first preserves evidence for model judgment. The second pretends the product has already made the coaching judgment.

## 4. What makes a historical session worth retaining

A session should receive higher historical resolution when at least one of these is true:

- it contains a progression target or a meaningful change from a prior exposure;
- it is a structured quality workout where segment-level execution matters;
- execution was partial, failed, missed, moved, or replaced;
- the same class of workout has repeatedly produced a notable outcome;
- the athlete explicitly reported a response that could matter to future programming;
- it is a benchmark / measurement reference;
- it is a strength movement with meaningful load / rep progression;
- it directly precedes a subsequent plan adjustment and therefore explains a change in programming.

This is a **retention criterion**, not a coaching rule. It decides what information is worth preserving for later retrieval; it does not decide what the coach should do.

## 5. Repeated failure is a first-class historical signal

A single failed workout is evidence.

Repeated failure of the same type is much stronger evidence and should become easy for the coach to retrieve.

The system should therefore preserve enough identity to answer questions such as:

- Have we tried this kind of interval before?
- How many recent exposures were completed as prescribed?
- Was the failure the same part of the workout each time?
- Did the athlete report the same response?
- What happened when the prescription was changed afterwards?

The system must not turn this into a fixed rule such as "two failures means reduce intensity." The repeated pattern is evidence that the model can weigh alongside current state and other constraints.

## 6. Long easy runs are intentionally cheap

A normal long easy run is important training history but usually low-information at the individual-session level.

Unless there is a meaningful deviation, it should normally be represented by a compact fact such as:

```text
long easy run | 92 min | completed | no reported issue
```

If the same long run instead:

- was shortened twice;
- produced an explicit recovery complaint;
- became progressively longer over several exposures;
- was deliberately used as a benchmark;
- or materially changed the next week's plan;

then it becomes a historical episode and earns more detail.

This is the intended asymmetry: **importance to training is not the same thing as context value per character.**

## 7. Decision relevance beats chronological breadth

The coach should not receive "the last 24 months" simply because that history exists.

For a current decision, history should be selected by relevance to the decision dimensions. Examples:

### Deciding whether to progress intervals

Prefer:

- recent interval / threshold / VO2 episodes;
- the same workout family at nearby doses;
- recent failed or partial quality sessions;
- relevant recovery / athlete response after those exposures;
- the current week's competing load.

Do not spend context on every ordinary easy run from six months ago.

### Deciding the next long run

Prefer:

- recent long-run exposures and progression;
- unusual recovery responses;
- recent lower-body load;
- current availability and goal.

A normal long run from months ago can usually remain aggregate background.

### Deciding strength progression

Prefer:

- the same movement's recent load / reps;
- failed or partial sets;
- athlete-reported response;
- the corresponding baseline evidence.

## 8. The context budget principle

Every field is part of one finite model-reading budget. A historical representation must therefore state what it buys.

The default priority is:

```text
current constraints / safety
    > current plan
    > recent response
    > relevant historical episodes
    > long-range aggregates
    > raw historical detail on demand
```

A new historical field is justified only when an eval demonstrates that the coach can make a materially better decision with it and the same value cannot be expressed more cheaply through an existing field.

This follows AGENTS.md §13 and §15: improve what the model reads before increasing what the model must choose between.

## 9. Phase boundary: keep the reviewed tool surface frozen

Phase 1 must not change:

- MCP tool names;
- tool input schemas;
- tool annotations;
- OpenAPI surface;
- Apps submission metadata;
- served tool catalogue.

Phase 1 may change:

- deterministic context projection;
- internal history representation;
- anonymous fixtures;
- eval cases and comparison packets;
- context-budget tests;
- documentation of evidence semantics.

The purpose is to make the coaching-evidence model testable while the product remains continuously reviewable through the existing OpenAI Apps submission.

Phase 2 begins only after Phase 1 answers the question: **what additional model-visible capability is actually required?** If a new tool is then necessary, it must have a concrete eval that fails before the tool and passes after it, and its Apps review implications are handled separately.

## 10. Phase 1 implementation sequence

### A. Inventory the current evidence

Map every existing context field to one of:

- current decision state;
- recent response;
- historical episode evidence;
- long-range aggregate;
- raw / queryable detail.

For each field record its consumer, temporal window, provenance, and budget.

### B. Define the episode projection

Do not persist a large copy of the original plan. Define the smallest deterministic projection that preserves:

- why the session mattered;
- what was prescribed;
- what actually happened;
- what the athlete explicitly reported;
- whether it influenced a later decision.

### C. Add contrastive evals

At minimum, compare:

1. current context only;
2. current context + raw historical sessions;
3. current context + compact relevant episodes.

Use the same coaching question and the same model. The test is whether the third representation produces better decisions without paying the context cost of the second.

Required sharp cases include:

- a progressing interval series;
- repeated failure of the same workout family;
- a long easy run that should remain low-resolution;
- a long run that becomes high-information because of an unusual response;
- a strength progression with a failed exposure followed by a successful adjustment.

Controls must include ordinary sessions that should **not** gain extra context merely because they are present in history.

### D. Only then change runtime context

Once the eval establishes the evidence shape, change the builder to serve that shape. The first runtime change should replace lower-value historical detail rather than simply append another history block.

## 11. Explicit non-goals

Phase 1 does not:

- calculate a training-readiness score;
- infer injury or medical diagnosis;
- assign a session quality score;
- infer causality from temporal sequence alone;
- prescribe a fixed progression percentage;
- make deterministic decisions about increasing or reducing training;
- expose raw provider payloads to the coach;
- add a new MCP tool.

The model remains responsible for coaching judgment. Deterministic code remains responsible for evidence acquisition, identity-backed reconciliation, provenance, validation, persistence, and bounded delivery.
