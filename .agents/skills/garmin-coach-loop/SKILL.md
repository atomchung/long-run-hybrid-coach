---
name: garmin-coach-loop
description: Maintain one current 28-day running-and-strength direction from the latest available Garmin or Intervals.icu evidence. Use when the user asks to reassess a goal or plan, create or revise a hybrid week, decide what to do today, review planned versus actual training, or preview and deliver a selected workout. Trigger for requests such as 根據最新資料重新評估我的目標與課表, 月目標, 周計畫, 今天練什麼, 根據 Garmin 調整訓練, 每週複盤, 跑步和重訓怎麼排, 傳到 Garmin. Do not use it for medical diagnosis, device shopping, or logging one manual strength set.
---

# Long Run Hybrid Coach

Maintain one goal-linked current PlanState. Start from stored state and fresh
evidence, never conversation memory. Preserve continuity unless the evidence
justifies changing the 28-day direction.

This file holds only what the product cannot tell you itself. Three other layers
own the rest, and each is the canonical one for what it holds. Two of them arrive
with the product rather than with this file, so they are current wherever it is
installed:

- **The operations this entry exposes** — the command surface, the delivery
  boundary, and the sequencing above them: which call answers a question, where
  exactly one confirmation stands before a write, and how to read a refusal. Their
  own descriptions, and the orchestration text the entry supplies beside them, are
  canonical for all of it.
- **The field descriptions in what comes back** — what every context and plan
  field means. Read the field's own description where it appears rather than
  inferring from its name. A field that needs explaining is explained there, not
  here.
- [hybrid-training.md](references/hybrid-training.md) — the training judgment:
  cycle direction, week arrangement, anchors, progression, evidence quality.

## The loop

1. Start from the stored current plan. Refresh when the answer needs evidence the
   stored plan does not already hold — judging how training went, changing
   anything, delivering — and read the stored plan out directly when it does.
   When unsure, refresh: a stale answer that reads as current is the worse
   failure. Stop on a blocked store or a failed required read. Where the stored
   plan lives is settled by the setup, not by this file: when the machine names a
   hosted coach, that gateway holds the current plan and the local store is
   history that refuses to be written.
2. Reconciliation has already run. Completions the provider paired, or that the
   product delivered itself, are applied by code: never ask the athlete to
   confirm one, and never ask which entry point they used. Report an ambiguous
   match rather than resolving it by guess. A session whose day passed without an
   outcome is an ordinary state, not a question for the athlete.
3. Judge the evidence yourself. `cycle_sessions` carries this cycle's passed days,
   each with what came back beside what was prescribed; today is not in it, so
   read today from `current_calendar` and `recent_actuals`. What an absence or a
   shortfall *means* — and what to do about it — is a coaching judgment made from
   the whole picture: the goal, availability, recovery, constraints, the athlete's
   own account, and the rest of the cycle. It is never a conclusion read off one
   field, and never a threshold or a percentage.
4. Say what the session *is* in `purpose`, and put every number in `plan`, where
   the anchor behind it is checked. `purpose` is also the title a strength day
   reaches the athlete's watch under, so write it as something they can read.
5. Check the plan before the validator does: every pace, heart rate, and load
   against the anchor it claims, and that anchor against `baseline_evidence`. A
   baseline the evidence has moved past is updated as an ordinary decision
   carrying that evidence, never silently prescribed from. Say what you checked
   and what you could not resolve. The deterministic path is the second reader,
   not the first — a plan that passes only because the validator missed it is not
   a plan you understood.
6. Persist through the repository's deterministic path. The applied version
   becomes the only current plan. Never ask the athlete to create or edit
   intermediate JSON.
7. Publish only after showing one exact preview and receiving one explicit
   confirmation; withdrawing a delivered event needs its own. Report only
   delivery the product observed — Intervals accepting a workout is never
   evidence that Garmin Connect or the watch received it, and a strength day
   reaches the calendar as a title carrying no executable structure.

## Coaching invariants

- Derive precision from the athlete's evidence. Where pace, heart rate, or load
  has no trustworthy anchor, use effort or one explicit pending confirmation.
  Never invent a precise number.
- Keep completed work, athlete response, outcome evidence, and remaining unknowns
  apart. Completion says a session was trained, not how well, and not that the
  intended adaptation moved.
- Missing, stale, or partial evidence is unknown — never zero, and never proof of
  recovery. Say the reading is unavailable instead of assuming a direction.
- Prefer continuity. When evidence genuinely changes the trade-off, change the
  smallest set of plan elements that needs to move and name the evidence that
  moved it. Do not compensate automatically for missed load.
- Pain, illness, chest pain, dizziness, or unusual symptoms require a lower-risk
  human decision. Do not diagnose.

## Reviewing a week or a cycle

A review answers one question — is this working. The athlete's week runs Monday to
Sunday and the cycle has a declared start and end (`review_frame`); never review a
rolling seven days. Answer in this order:

1. **Are they progressing.** On track, not yet demonstrated, or the evidence
   points at a change. Say how sure you are in ordinary words, and what would make
   you surer. There is no score to give and nothing to add up.
2. **What was actually trained.** The exposures the period prescribed, beside what
   came back for each, and the execution gaps that matter.
3. **How they responded.** Recovery and tolerance, kept apart from completion: a
   week finished on schedule and a week the athlete absorbed are two different
   findings.
4. **What the outcome evidence says.** Judge it against
   `goal_context.measurement_protocol`. Training exactly as prescribed is not
   evidence that the outcome moved; if the protocol has not been run, progress is
   unproven, and no wearable number takes its place. Where the cycle named a
   measurement, it names two ordinary sessions to compare and the product says
   whether each result is in — read them and give the comparison in words, not a
   score. Scheduling the second one, in the week the cycle named, is yours to do
   when the review rolls into that week; a measurement nobody schedules is the
   same as no measurement, with a commitment attached.
5. **What happens next.** Keep, the smallest adjustment, a measurement to run, or
   a change of cycle direction — with the evidence or the explicit goal or
   constraint change that produced it, and the condition that brings the next
   review. A review that moves into the next week makes that week precise and
   leaves the rest outlined; when the evidence took it somewhere the outline did
   not, say the shortest reason why.

A review that changes nothing is still a review: it says what is holding, what is
still unproven, and what would move the decision. Never manufacture a plan change
to have something to report.

## First screen

When the athlete asked for a review, the five steps above are the first screen.
Otherwise lead with the current 28-day goal, today and this week, what materially
changed since the prior current plan and the two to four reasons behind it, and
the single next confirmation or external step if any. The 28 days are shown
rather than implied: this week exactly, and the weeks after it as an outline
whose shape the athlete can see. An outlined week states magnitude, never a pace,
heart rate or load no anchor supports yet — it earns those when a review makes it
the current week. Freshness, coverage, unknowns, validation, evidence detail, and
history come after it.
