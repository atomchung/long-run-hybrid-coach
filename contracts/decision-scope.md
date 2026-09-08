# Plan-authoring decision scope

`prepareCoachDecision.change_request.decision_scope` declares the decision the
athlete is considering. The current tool schema requires `week` or `cycle`.
The mapping is owned by `garmin_coach_loop.decision_scope.DECISION_SCOPE_MODES`.

- `week` maps to `review_week`. It may adjust this week's sessions, week metadata,
  athlete baseline and remaining cycle outlook. It preserves the goal and every
  other cycle field, including the 28-day dates.
- `cycle` maps to `review_cycle`. A goal or cycle reassessment may also change the
  executable current week in one preview and one confirmed, atomic application.
- A first plan declares `cycle` and creates a `plan_cycle` event.

The preview returns the effective `decision_scope` beside the before/after values.
The existing proposal binds that preview and event. Changing scope after preview
requires a new preview and confirmation; it does not apply the revised decision.
Scope does not authorize provider delivery or override symptom, identity,
provenance, evidence or structural validation. Missing evidence stays unknown.

Local `apply-decision` already accepts a complete DecisionEvent. Its `mode` is the
explicit scope: `plan_week`/`review_week` preserve goal and cycle direction;
`plan_cycle`/`review_cycle` permit their week to change with them. No second scope
field is added to persisted events or historical stores.

## Compatibility with an older tool catalogue

The server accepts an omitted field from clients that have not refreshed their
catalogue. Omission follows the existing conservative rule: changed cycle dates
map to `review_cycle`; otherwise a changed week maps to `review_week`; otherwise a
goal/cycle change maps to `review_cycle`. A combined goal/cycle and week change
within the same 28-day window remains refused on that legacy path. An omitted
scope on a first plan retains its existing `plan_cycle` behavior. Explicit null,
unknown values and week-scoped first plans are invalid, not omissions.

## Boundary and cost

The invariant is authorization scope: a weekly adjustment must not quietly rewrite
the goal or 28-day direction. A warning cannot preserve that boundary once the
replacement goal has been stored. The existing week-mode validator is the narrow
owner of the check; no coaching heuristic is added. The false-positive cost is
that an old client cannot express an atomic in-window goal/cycle-plus-week change
until it declares scope. Current clients can express it directly, while week-only,
cycle-only, baseline and missing-evidence workflows remain available. Regression
tests pair week-scope refusals with the same valid diff under cycle scope.

The one input enum and one preview field buy intent that no diff can establish.
They reuse existing event modes and confirmation rather than adding a tool or a
stored state. The field description replaces the outcome-only goal description;
no training judgment or orchestration rule is needed to define its semantics.
