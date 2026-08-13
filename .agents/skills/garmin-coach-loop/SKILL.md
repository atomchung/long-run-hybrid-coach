---
name: garmin-coach-loop
description: Maintain one current 28-day running-and-strength direction from the latest available Garmin or Intervals.icu evidence. Use when the user asks to reassess a goal or plan, create or revise a hybrid week, decide what to do today, review planned versus actual training, or preview and deliver a selected running workout. Trigger for requests such as 根據最新資料重新評估我的目標與課表, 月目標, 周計畫, 今天練什麼, 根據 Garmin 調整訓練, 每週複盤, 跑步和重訓怎麼排, 傳到 Garmin. Do not use it for medical diagnosis, device shopping, or logging one manual strength set.
---

# Garmin Coach Loop

Maintain one goal-linked current PlanState. Start from stored state and fresh
evidence, never conversation memory. Preserve continuity unless the evidence
justifies changing the 28-day direction.

[README.md](../../../README.md) owns the deterministic command surface, local
state mechanics, provider setup, and delivery boundary. Do not duplicate those
instructions here. Read
[hybrid-training.md](references/hybrid-training.md) when making a cycle, week, or
progression judgment.

## One product path

1. Start from the stored current plan and run the repository's deterministic
   refresh path. Stop on a blocked store or failed required read.
2. Use the refreshed context and post-reconciliation current plan. Completions
   backed by the provider's pairing or by the product's own delivered session are
   already applied by code; never ask the athlete to confirm them, and never ask
   which entry point they used. A completed session means it was trained, not how
   well — read that from what came back. `cycle_sessions` puts the two side by
   side: one row per session of this cycle whose day has passed, carrying its
   prescription, planned minutes, last `match_status`, and the activity that
   attached to it. For strength, the day's logged sets are in
   `strength_execution` under the same date, one entry per exercise — three sets
   of five, or a last set that dropped 5 kg, is the athlete telling you the load
   was too high. Today's own session is not in `cycle_sessions`; read it from
   `current_calendar` and `recent_actuals`. Report ambiguous matches without
   guessing. A session whose day passed without an outcome is an ordinary
   state, not a question for the athlete: judge whether that work still matters to
   the week ahead, reschedule it when it does, and let it go when it does not.
3. Reassess the existing goal before changing it. Select or update one 28-day
   primary adaptation, one maintenance direction, a measurement approach, and
   explicit adjust／stop conditions.
4. Build this week's hybrid plan from actual availability, recent work, recovery,
   and the athlete's own baseline. Start from the shape their recent weeks
   already hold and state any departure from it with the evidence behind it.
   Read `cycle_sessions` as part of that evidence, and read it twice. A row whose
   `activity_evidence` is `none_found` was scheduled and not trained: the week is
   too full, so drop density rather than intensity. A row whose activity or logged
   sets fall short of its prescription was started and not finished: the load is
   too high, so drop weight or pace and keep the density. Which sessions they are
   says more than how many, and the same count can mean either. `attached` says
   what attached, not how well it went; `other_activity_same_day` and
   `outside_evidence_window` are facts about the data, never about the athlete.
   Weigh all of it; never turn it into a threshold or a percentage. Protect the
   primary work while managing running, lower-body strength, recovery, time, and
   equipment conflicts.
5. Make every session executable:
   - a running session's executable target lives in `structured_workout` —
     duration／distance, structure, recovery, and one supported target. An easy,
     recovery, or long run's heart-rate target is a structured `hr_ceiling`
     (absolute bpm, ceiling only) so the watch enforces it; pace stays the
     outcome, not the target;
   - `prescription` is the human-readable summary of that session, written in the
     athlete's own language. It carries no required wording and never overrides
     the structure;
   - a strength session's prescribed work lives in `strength_movements` — one
     entry per movement, named with the same canonical exercise key its baseline
     uses, carrying sets, reps (null for a set taken to failure), and a
     `load_basis` that says whether the load is baseline-measured, bodyweight, or
     a confirmation still owed. It stays on the plan; strength still reaches the
     calendar as a title only. A session written without it is read from its
     prescription instead, which then has to carry exercise, sets, reps, and a
     baseline-supported load or one explicit load confirmation still needed.
6. Check the plan against the evidence before validating it: every pace, heart
   rate, and load against the anchor it claims; the week against the adaptation
   it is supposed to protect; each change against the reason given for it. Say
   what you checked and what you could not resolve. The deterministic path is
   the second reader, not the first — a plan that only passes because the
   validator did not catch it is not a plan you understood.
7. Validate and persist the selected result through the repository's
   deterministic path. The applied version becomes the only current plan for the
   next revisit or review. Never ask the athlete to create or edit intermediate
   JSON.
8. For publishable running workouts, show one exact preview. After one explicit
   confirmation, use the deterministic delivery path for approval, deduplication,
   write, read-back verification, and current-state update. Strength stays text.

## Coaching judgment

- Derive precision from the athlete's evidence. If pace, heart rate, or strength
  load lacks a trustworthy anchor, use effort or one explicit pending
  confirmation; never invent a precise number.
- When context carries `strength_execution` evidence, judge load progression from
  the sets actually completed there, not from the written `athlete_baseline`
  figure alone; when it is `null`, say so and treat baseline precision as
  unverified.
- When context carries `recovery_signals` evidence, read a failed or unusually
  heavy session against that day's recovery state (readiness, HRV status, acute
  load, Body Battery, stress) before reading it as a capacity problem; when it is
  `null`, say the recovery reading is unavailable rather than assuming either way.
- Compare planned with actual before interpreting wearable recovery. A single
  sleep, HRV, resting-heart-rate, readiness, weight, or completion value does not
  decide alone.
- Prefer continuity when new evidence does not change the trade-off. When it
  does, change the smallest set of plan elements that genuinely needs to move.
- Do not compensate automatically for missed load or infer improved fitness from
  completion alone. Separate completed work, athlete response, outcome evidence,
  and remaining unknowns.
- Pain, illness, chest pain, dizziness, or unusual symptoms require a lower-risk
  human decision. Do not diagnose.

## First screen

Lead with:

1. **Current goal** — the 28-day primary and maintenance direction.
2. **Today and this week** — executable running and strength prescriptions.
3. **What changed** — only material differences from the prior current plan and
   the two to four reasons that changed the decision.
4. **Delivery** — the observed state of publishable running workouts and the
   single next confirmation or external step, if any.

Put freshness, coverage, unknowns, validation, evidence details, and history
after this first screen. Never claim a Garmin Connect or watch hop that the
product did not observe.
