"""Project one coaching change request onto the current PlanState.

AGENTS.md invariant 4 splits the work: the model owns coaching judgment, deterministic
code owns everything mechanical. The agent-facing HTTP contract did not honour that split.
It asked for a complete candidate PlanState and a complete DecisionEvent, which are mostly
mechanical -- every unchanged field copied verbatim, plan and event versions, event
identity, hashes, timestamps, delivery bookkeeping -- and only incidentally coaching. A
model that has to rebuild all of that in order to move one session will eventually rebuild
it slightly wrong, and the validator will refuse a change the athlete actually wanted.

So the contract here carries coaching judgment only:

- which sessions to keep, move, reduce, replace, or add, and what each should now say;
- what the week, the goal, and the 28-day cycle should say;
- why: a summary, the evidence behind it, its effect, what to watch, what is unknown.

Everything else is derived here from the PlanState the store already holds. Notably:

- unchanged sessions and unchanged plan fields are *copied*, never re-authored;
- ``hard`` follows ``cost``, and ``publish_supported`` follows what delivery could
  actually send for a session, so neither can be claimed;
- ``prescription`` is rendered from the session's own plan, so the sentence the athlete
  reads and the structure the product checks can never be two different sessions;
- a change that invalidates an already-delivered workout resets that session's delivery
  observation, which is the transition the validator demands and a model kept forgetting;
- plan version, event id, event mode/action, timestamps and provenance are server-owned.

This module decides nothing about safety. It builds a candidate, and ``validate_bundle``
remains the only authority on whether that candidate may be adopted -- which is why there
is no threshold, no load rule, and no second opinion about coaching anywhere below. The
few refusals here are about the request being projectable at all: an operation naming a
session that does not exist, a "reduce" that raises the number it reduces, a running
session whose executable content changed without saying what it changed to.
"""

from __future__ import annotations

import copy
import datetime as dt
import json
from typing import Any

from .delivery_content import delivery_session_content
from .prescription import DEFAULT_LANGUAGE, render_prescription
from .store import canonical_hash
from .validation import (
    ADAPTATIONS,
    ATHLETE_BASELINE_INTEGER_FIELDS,
    ATHLETE_BASELINE_NUMBER_FIELDS,
    BODY_STRESS,
    COSTS,
    DECISION_EVENT_SCHEMA_VERSION,
    PRIORITIES,
    REASON_CODES,
    SPORTS,
    STRENGTH_LOAD_FIELDS,
    STRENGTH_LOAD_OPTIONAL_FIELDS,
    anchoring_baseline,
)


# The two mechanical reason codes are written only by the reconciler and by the verified
# delivery boundary. A coaching change request may never claim either, because both carry
# deterministic semantics this projection does not satisfy.
COACHING_REASON_CODES = REASON_CODES - {"planned_actual_reconciled", "delivery_verified"}

# No "remove". A session is kept, moved, reduced, or replaced -- dropping one silently is
# how a week quietly loses training nobody decided to drop. Replacing it with a rest or
# recovery session says the same thing and leaves the decision visible in history.
OPERATIONS = ("keep", "move", "reduce", "replace", "add")

_REQUIRED_FIELDS = (
    "summary",
    "reason_codes",
    "evidence",
    "goal_effect",
    "next_review_condition",
)
_OPTIONAL_FIELDS = ("unknowns", "sessions", "goal", "cycle", "week", "athlete_baseline")

_FALLBACK_ACTIONS = {"reduce", "move", "replace", "rest"}
_CYCLE_FIELDS = (
    "start",
    "end",
    "primary_adaptation",
    "maintenance_adaptation",
    "planned_evidence",
    "adjust_conditions",
    "stop_conditions",
    "outlook",
)
_WEEK_FIELDS = ("start", "intent")
_OUTLOOK_FIELDS = ("week_start", "intent", "key_sessions", "relation_to_primary")
_MEASUREMENT_FIELDS = ("reference_session_id", "measurement_week_start", "compare")

# No operation accepts `prescription`: it is rendered from `plan` (issue #93), so a
# request that carried one would be authoring the sentence the structure already says.
# `plan` is required wherever a session is created or re-decided, and optional on
# `reduce`, which may be lowering the duration of work whose structure is unchanged.
# `purpose` is optional on `keep` alone among the untouched-schedule operations: nothing
# about the session's training moves, so match_status stays planned rather than the
# replaced a full `replace` would stamp on a rewording that changed nothing else.
# `coach_note` is optional on every operation, which is the only placement that works: the
# sentence is about the session, not about the change, and the case it exists for --
# "this week's long run is deliberately short, do not add to it" -- is most often a `keep`
# or a `reduce` rather than something the coach was rewriting anyway. Null clears it, the
# way null clears `measures` on a replace, because a note that no longer applies has to be
# removable without rewriting the session it sits on.
_COACH_NOTE = ("coach_note",)
_OPERATION_FIELDS: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    "keep": (("session_id",), ("purpose", *_COACH_NOTE)),
    "move": (("session_id", "scheduled_date"), ("time_window", *_COACH_NOTE)),
    "reduce": (
        ("session_id", "planned_minutes"),
        ("purpose", "plan", *_COACH_NOTE),
    ),
    "replace": (
        ("session_id", "purpose", "adaptation", "cost", "planned_minutes", "plan"),
        (
            "sport",
            "scheduled_date",
            "body_stress",
            "priority",
            "time_window",
            "fallback",
            # Only on the two operations that decide what a session *is*. A move or a
            # reduce changes when or how much, and neither turns a session into the
            # cycle's measurement point or stops it being one.
            "measures",
            *_COACH_NOTE,
        ),
    ),
    "add": (
        (
            "sport",
            "scheduled_date",
            "purpose",
            "adaptation",
            "body_stress",
            "cost",
            "priority",
            "planned_minutes",
            "plan",
            "fallback",
        ),
        ("time_window", "measures", *_COACH_NOTE),
    ),
}

# What the athlete has to see to recognise their own week in a preview: when it happens,
# what it is, how long, how hard, and the prescription in their own wording.
_PREVIEW_SESSION_FIELDS = (
    "session_id",
    "scheduled_date",
    "sport",
    "adaptation",
    "cost",
    "priority",
    "planned_minutes",
    "prescription",
    "time_window",
    "match_status",
    # What the coach said about this session, shown in the same preview the athlete
    # confirms. A note they never saw is a note they never agreed to hand to their own
    # watch, and it is the half of the preview that says *why* the week looks like this.
    "coach_note",
)


class ChangeRequestError(RuntimeError):
    """A change request cannot be projected onto the current PlanState."""


# --------------------------------------------------------------------------------------
# Request-shape helpers. Every one fails closed rather than coercing.
# --------------------------------------------------------------------------------------


def _object(value: Any, field: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ChangeRequestError(f"{field} must be an object")
    return value


def _array(value: Any, field: str) -> list[Any]:
    if not isinstance(value, list):
        raise ChangeRequestError(f"{field} must be an array")
    return value


def _keys(
    value: dict[str, Any],
    field: str,
    required: tuple[str, ...],
    optional: tuple[str, ...] = (),
) -> None:
    missing = sorted(set(required) - value.keys())
    if missing:
        raise ChangeRequestError(f"{field} is missing {', '.join(missing)}")
    unexpected = sorted(value.keys() - (set(required) | set(optional)))
    if unexpected:
        raise ChangeRequestError(f"{field} does not accept {', '.join(unexpected)}")


def _text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ChangeRequestError(f"{field} must be a non-empty string")
    return value


def _enum(value: Any, field: str, allowed: set[str]) -> str:
    if value not in allowed:
        raise ChangeRequestError(f"{field} must be one of {', '.join(sorted(allowed))}")
    return str(value)


def _minutes(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ChangeRequestError(f"{field} must be an integer >= 0")
    return value


def _date(value: Any, field: str) -> str:
    try:
        return dt.date.fromisoformat(value).isoformat()
    except (TypeError, ValueError):
        raise ChangeRequestError(f"{field} must be an ISO date, for example 2026-08-20") from None


def _text_array(value: Any, field: str, *, minimum: int = 0) -> list[str]:
    items = _array(value, field)
    if len(items) < minimum:
        raise ChangeRequestError(f"{field} must contain at least {minimum} item(s)")
    return [_text(item, f"{field}[{index}]") for index, item in enumerate(items)]


def _sessions_by_id(plan: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        session["session_id"]: session
        for session in ((plan.get("week") or {}).get("sessions") or [])
        if isinstance(session, dict) and isinstance(session.get("session_id"), str)
    }


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def _utc_iso(moment: dt.datetime) -> str:
    return (
        moment.astimezone(dt.timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


# --------------------------------------------------------------------------------------
# The coaching half of the request: prose, evidence, and the plan-level intent
# --------------------------------------------------------------------------------------


def _evidence(value: Any) -> list[dict[str, str]]:
    items = _array(value, "change_request.evidence")
    if not items:
        raise ChangeRequestError("change_request.evidence must cite at least one observation")
    evidence = []
    for index, raw in enumerate(items):
        field = f"change_request.evidence[{index}]"
        item = _object(raw, field)
        _keys(item, field, ("field", "observation"))
        evidence.append(
            {
                "field": _text(item.get("field"), f"{field}.field"),
                "observation": _text(item.get("observation"), f"{field}.observation"),
            }
        )
    return evidence


def _goal_effect(value: Any) -> dict[str, str]:
    effect = _object(value, "change_request.goal_effect")
    _keys(effect, "change_request.goal_effect", ("week", "cycle"))
    return {
        "week": _text(effect.get("week"), "change_request.goal_effect.week"),
        "cycle": _text(effect.get("cycle"), "change_request.goal_effect.cycle"),
    }


def _reason_codes(value: Any) -> list[str]:
    codes = _text_array(value, "change_request.reason_codes", minimum=1)
    for index, code in enumerate(codes):
        _enum(code, f"change_request.reason_codes[{index}]", COACHING_REASON_CODES)
    if len(set(codes)) != len(codes):
        raise ChangeRequestError("change_request.reason_codes must not repeat a code")
    return codes


def _apply_goal(after: dict[str, Any], value: Any) -> None:
    if value is None:
        return
    goal = _object(value, "change_request.goal")
    _keys(goal, "change_request.goal", ("outcome", "measurement_protocol"), ("measurement",))
    rewritten = {
        "outcome": _text(goal.get("outcome"), "change_request.goal.outcome"),
        "measurement_protocol": _text(
            goal.get("measurement_protocol"), "change_request.goal.measurement_protocol"
        ),
    }
    if goal.get("measurement") is not None:
        rewritten["measurement"] = _measurement(
            goal["measurement"], "change_request.goal.measurement"
        )
    after["goal"] = rewritten


def _apply_cycle(after: dict[str, Any], value: Any) -> None:
    """Merge a partial cycle change onto the current one.

    Partial on purpose: changing the primary adaptation should not require restating the
    stop conditions, and restating them is how they drift.
    """
    if value is None:
        return
    cycle = _object(value, "change_request.cycle")
    _keys(cycle, "change_request.cycle", (), _CYCLE_FIELDS)
    if not cycle:
        raise ChangeRequestError("change_request.cycle must name at least one cycle field")
    current = dict(after.get("cycle") or {})
    for field in ("start", "end"):
        if field in cycle:
            current[field] = _date(cycle[field], f"change_request.cycle.{field}")
    if "primary_adaptation" in cycle:
        current["primary_adaptation"] = _enum(
            cycle["primary_adaptation"], "change_request.cycle.primary_adaptation", ADAPTATIONS
        )
    if "maintenance_adaptation" in cycle:
        maintenance = cycle["maintenance_adaptation"]
        current["maintenance_adaptation"] = (
            None
            if maintenance is None
            else _enum(
                maintenance, "change_request.cycle.maintenance_adaptation", ADAPTATIONS
            )
        )
    for field in ("planned_evidence", "adjust_conditions", "stop_conditions"):
        if field in cycle:
            current[field] = _text_array(
                cycle[field], f"change_request.cycle.{field}", minimum=1
            )
    if "outlook" in cycle:
        current["outlook"] = _outlook(cycle["outlook"], "change_request.cycle.outlook")
    after["cycle"] = current


def _measurement(value: Any, field: str) -> dict[str, Any]:
    """The runnable half of the cycle's protocol (issues #13, #75).

    Three facts and no verdict: which ordinary session this cycle measures against, which
    of its weeks repeats that session, and what to hold constant while reading the two.
    Whether the second reading is better than the first is not in here and is not
    computed anywhere -- it is what the coach answers at the review.
    """
    measurement = _object(value, field)
    _keys(measurement, field, _MEASUREMENT_FIELDS)
    return {
        "reference_session_id": _text(
            measurement.get("reference_session_id"), f"{field}.reference_session_id"
        ),
        "measurement_week_start": _date(
            measurement.get("measurement_week_start"), f"{field}.measurement_week_start"
        ),
        "compare": _text(measurement.get("compare"), f"{field}.compare"),
    }


def _outlook(value: Any, field: str) -> list[dict[str, Any]]:
    """The rest of the cycle as the athlete will see it (issue #61).

    Shape only. The dates are checked for being dates and the strings for being
    non-empty; whether the weeks are the right weeks, and whether their shape is the
    right shape, are checked once by the validator and decided once by the coach.

    Note what an entry cannot hold: no session id, no execution, no prescription. That
    is what keeps an outlined week out of delivery, reconciliation and staleness without
    any of those paths needing to know it exists.
    """
    weeks: list[dict[str, Any]] = []
    for index, raw in enumerate(_array(value, field)):
        item_field = f"{field}[{index}]"
        item = _object(raw, item_field)
        _keys(item, item_field, _OUTLOOK_FIELDS)
        weeks.append(
            {
                "week_start": _date(item.get("week_start"), f"{item_field}.week_start"),
                "intent": _text(item.get("intent"), f"{item_field}.intent"),
                "key_sessions": _text_array(
                    item.get("key_sessions"), f"{item_field}.key_sessions", minimum=1
                ),
                "relation_to_primary": _text(
                    item.get("relation_to_primary"), f"{item_field}.relation_to_primary"
                ),
            }
        )
    return weeks


def _baseline_integer(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ChangeRequestError(f"{field} must be an integer >= 1")
    return value


def _baseline_number(value: Any, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        raise ChangeRequestError(f"{field} must be a number >= 0")
    return value


def _upsert_strength_loads(
    current: list[dict[str, Any]], value: Any
) -> list[dict[str, Any]]:
    """Update or add the movements the request names; every other movement stays.

    Upsert rather than whole-list replacement for the same reason OPERATIONS has no
    "remove": a restated list is how a movement nobody decided to drop goes missing,
    and with it the anchor every future prescription of that lift checks against.
    Matching reuses ``anchoring_baseline`` -- the one resolution of a movement name to
    its baseline entry -- so "bench_press" and a display name reach the same entry
    here exactly as they do everywhere else.

    Within an entry, ``load_kg`` and ``assist_kg`` move as one pair: they are the two
    directions of a single measurement, so naming either replaces both (the absent one
    to null), which is how an assisted lift becomes a loaded one without dragging the
    old assistance along. ``scheme`` and ``display_name`` are kept from the existing
    entry when not restated. There is no removal: forgetting a lift is a local
    `set-baseline` act, never a side effect of updating another one.
    """
    field = "change_request.athlete_baseline.strength_loads"
    entries = value if isinstance(value, list) else None
    if not entries:
        raise ChangeRequestError(f"{field} must be a non-empty array")
    result = [dict(load) for load in current]
    for index, raw in enumerate(entries):
        item_field = f"{field}[{index}]"
        item = _object(raw, item_field)
        _keys(item, item_field, ("exercise",), STRENGTH_LOAD_OPTIONAL_FIELDS + (
            "load_kg", "assist_kg", "scheme",
        ))
        exercise = _text(item.get("exercise"), f"{item_field}.exercise")
        if "load_kg" not in item and "assist_kg" not in item and "scheme" not in item:
            raise ChangeRequestError(
                f"{item_field} must carry load_kg, assist_kg, or scheme -- an exercise "
                "name alone changes nothing"
            )
        load_kg = item.get("load_kg")
        assist_kg = item.get("assist_kg")
        if load_kg is not None:
            load_kg = _baseline_number(load_kg, f"{item_field}.load_kg")
        if assist_kg is not None:
            assist_kg = _baseline_number(assist_kg, f"{item_field}.assist_kg")
        if ("load_kg" in item or "assist_kg" in item) and load_kg is None and assist_kg is None:
            raise ChangeRequestError(
                f"{item_field} must carry a measured load_kg or assist_kg -- clearing "
                "a measurement back to unknown is a local set-baseline act"
            )
        entry = anchoring_baseline(exercise, result)
        if entry is None:
            entry = {name: None for name in STRENGTH_LOAD_FIELDS}
            entry["exercise"] = exercise
            result.append(entry)
        if "load_kg" in item or "assist_kg" in item:
            entry["load_kg"] = load_kg
            entry["assist_kg"] = assist_kg
        if "scheme" in item:
            entry["scheme"] = (
                None
                if item.get("scheme") is None
                else _text(item.get("scheme"), f"{item_field}.scheme")
            )
        if "display_name" in item:
            entry["display_name"] = _text(
                item.get("display_name"), f"{item_field}.display_name"
            )
    return result


def _apply_athlete_baseline(after: dict[str, Any], value: Any) -> None:
    """Merge a partial baseline change onto the current one.

    Partial like ``cycle``, and for the same reason: updating the bench figure should
    not require restating the threshold pace, and restating it is how it drifts. A
    scalar arrives as a measured value, never null -- clearing an anchor back to
    "not known" is a local `set-baseline` act, and refusing null here is what keeps a
    model that pads every field with null from silently wiping anchors it never meant
    to touch. What the new figure should be, and whether the evidence justifies it,
    stays coaching judgment held by `validate_bundle` like every other change.
    """
    if value is None:
        return
    field = "change_request.athlete_baseline"
    baseline = _object(value, field)
    scalar_fields = ATHLETE_BASELINE_INTEGER_FIELDS + ATHLETE_BASELINE_NUMBER_FIELDS
    _keys(baseline, field, (), scalar_fields + ("strength_loads",))
    if not baseline:
        raise ChangeRequestError(f"{field} must name at least one baseline field")
    current = copy.deepcopy(after.get("athlete_baseline") or {})
    for name in scalar_fields:
        current.setdefault(name, None)
    current.setdefault("strength_loads", [])
    for name in ATHLETE_BASELINE_INTEGER_FIELDS:
        if name in baseline:
            current[name] = _baseline_integer(baseline[name], f"{field}.{name}")
    for name in ATHLETE_BASELINE_NUMBER_FIELDS:
        if name in baseline:
            current[name] = _baseline_number(baseline[name], f"{field}.{name}")
    if "strength_loads" in baseline:
        current["strength_loads"] = _upsert_strength_loads(
            current.get("strength_loads") or [], baseline["strength_loads"]
        )
    after["athlete_baseline"] = current


def _apply_week(after: dict[str, Any], value: Any) -> None:
    if value is None:
        return
    week = _object(value, "change_request.week")
    _keys(week, "change_request.week", (), _WEEK_FIELDS)
    if not week:
        raise ChangeRequestError("change_request.week must name at least one week field")
    if "start" in week:
        after["week"]["start"] = _date(week["start"], "change_request.week.start")
    if "intent" in week:
        after["week"]["intent"] = _text(week["intent"], "change_request.week.intent")


def _roll_past_sessions(
    before: dict[str, Any], after: dict[str, Any], records: list[dict[str, str]]
) -> list[dict[str, Any]]:
    """Let the week the plan holds move on, leaving the days it passed behind.

    The plan holds exactly one week, so a start that moves forward is the ordinary
    weekly review. Work still ahead of the athlete moves with it -- that is a ``move``,
    and it is theirs to do on its new day. What cannot move is a day that already
    happened: rescheduling a session the athlete already did would report last week's
    training as next week's plan. Those sessions belong to the commit chain from here on,
    which is where ``store.cycle_sessions`` reads a cycle's whole record back from, and
    carrying them instead would leave each one outside the week it claims to be in, with
    no operation able to fix it.

    So this runs after the operations, never instead of them: whatever the request moved
    into the new week stays, and only what the new week has already passed is left
    behind. It leaves in whatever state it was last written with, including a delivered
    workout's id -- the athlete did that day, and nothing about it is withdrawn from
    their calendar.

    What leaves is returned rather than simply dropped, because the reason there is no
    ``remove`` operation applies to a roll exactly as it applies inside a week: a session
    that disappears without the athlete being told is training their week lost that
    nobody decided to lose. The caller puts these in the preview and in the event's own
    narrative, so the roll is named in the record instead of being read off a shrunken
    total.

    A session this same request operated on may not leave this way. The request said what
    should happen to it and the roll would then overrule that silently -- so it is
    refused, with both dates in the message, and the coach either moves it into the new
    week or leaves it to the week it belongs to.
    """
    previous = (before.get("week") or {}).get("start")
    start = (after.get("week") or {}).get("start")
    if not isinstance(previous, str) or not isinstance(start, str) or start <= previous:
        return []
    carried: list[dict[str, Any]] = []
    rolled: list[dict[str, Any]] = []
    for session in after["week"].get("sessions") or []:
        if not isinstance(session, dict) or str(session.get("scheduled_date", "")) >= start:
            carried.append(session)
            continue
        rolled.append(session)
    touched = {record["session_id"] for record in records}
    for session in rolled:
        session_id = str(session.get("session_id"))
        if session_id in touched:
            raise ChangeRequestError(
                f"change_request operates on {session_id}, dated "
                f"{session.get('scheduled_date')}, which the week starting {start} has "
                "already passed; move it into the new week or leave it to the week it is in"
            )
    after["week"]["sessions"] = carried
    return rolled


# --------------------------------------------------------------------------------------
# The session operations
# --------------------------------------------------------------------------------------


def _fallback(value: Any, field: str) -> dict[str, str]:
    fallback = _object(value, field)
    _keys(fallback, field, ("action", "description"))
    return {
        "action": _enum(fallback.get("action"), f"{field}.action", _FALLBACK_ACTIONS),
        "description": _text(fallback.get("description"), f"{field}.description"),
    }


def _new_session_id(sport: str, scheduled_date: str, taken: set[str]) -> str:
    """A stable id for a session the coach just added.

    Derived from what the session *is* rather than from a counter or a clock, so preparing
    and applying the same request twice name the same session both times.
    """
    candidate = f"{sport}-{scheduled_date}"
    suffix = 1
    while candidate in taken:
        suffix += 1
        candidate = f"{sport}-{scheduled_date}-{suffix}"
    return candidate


def _added_session(
    op: dict[str, Any], field: str, taken: set[str], language: str
) -> dict[str, Any]:
    sport = _enum(op.get("sport"), f"{field}.sport", SPORTS)
    scheduled_date = _date(op.get("scheduled_date"), f"{field}.scheduled_date")
    cost = _enum(op.get("cost"), f"{field}.cost", COSTS)
    session: dict[str, Any] = {
        "session_id": _new_session_id(sport, scheduled_date, taken),
        "sport": sport,
        "scheduled_date": scheduled_date,
        "time_window": (
            None
            if op.get("time_window") is None
            else _text(op.get("time_window"), f"{field}.time_window")
        ),
        "purpose": _text(op.get("purpose"), f"{field}.purpose"),
        "adaptation": _enum(op.get("adaptation"), f"{field}.adaptation", ADAPTATIONS),
        "body_stress": _enum(op.get("body_stress"), f"{field}.body_stress", BODY_STRESS),
        "cost": cost,
        "priority": _enum(op.get("priority"), f"{field}.priority", PRIORITIES),
        "planned_minutes": _minutes(op.get("planned_minutes"), f"{field}.planned_minutes"),
        "hard": cost == "hard",
        "plan": _object(op.get("plan"), f"{field}.plan"),
        "fallback": _fallback(op.get("fallback"), f"{field}.fallback"),
        "execution": {
            "publish_supported": False,
            "external_id": None,
            "delivery_state": "not_published",
        },
        "match_status": "planned",
    }
    if op.get("measures") is not None:
        session["measures"] = _text(op["measures"], f"{field}.measures")
    _coach_note(session, op, field)
    # Rendered, never taken from the request: two sessions with the same plan read the
    # same way, and validation refuses any other value.
    session["prescription"] = render_prescription(session["plan"], language)
    return session


def _coach_note(session: dict[str, Any], op: dict[str, Any], field: str) -> None:
    """Set, clear or leave alone the sentence the coach wants beside this session.

    One helper for all five operations, because the field means the same thing on every
    one of them: absent leaves whatever the session held, a string replaces it, and null
    removes it. That last case is why the field is nullable at all -- a note explaining a
    deliberate cutback outlives its reason, and the coach has to be able to take it off
    without rewriting the session to do it.
    """
    if "coach_note" not in op:
        return
    if op["coach_note"] is None:
        session.pop("coach_note", None)
    else:
        session["coach_note"] = _text(op["coach_note"], f"{field}.coach_note")


def _keep(session: dict[str, Any], op: dict[str, Any], field: str) -> None:
    """Reword a session's purpose while claiming no other change.

    ``keep`` is otherwise a no-op by construction -- the session is found and left alone
    -- so this is the one place an athlete-facing label can move without also moving the
    schedule, the structure, or match_status. A coach who finds the watch title unclear
    has exactly this path: everything about training stays kept, only the description of
    it changes.
    """
    if "purpose" in op:
        session["purpose"] = _text(op["purpose"], f"{field}.purpose")
    _coach_note(session, op, field)


def _move(session: dict[str, Any], op: dict[str, Any], field: str) -> None:
    session["scheduled_date"] = _date(op.get("scheduled_date"), f"{field}.scheduled_date")
    if "time_window" in op:
        session["time_window"] = (
            None
            if op["time_window"] is None
            else _text(op["time_window"], f"{field}.time_window")
        )
    _coach_note(session, op, field)
    session["match_status"] = "moved"


def _reduce(session: dict[str, Any], op: dict[str, Any], field: str) -> None:
    minutes = _minutes(op.get("planned_minutes"), f"{field}.planned_minutes")
    current = session.get("planned_minutes")
    if isinstance(current, int) and not isinstance(current, bool) and minutes >= current:
        raise ChangeRequestError(
            f"{field} reduces {session['session_id']} to {minutes} minutes, which is not "
            f"below its current {current}; use replace to prescribe something different"
        )
    session["planned_minutes"] = minutes
    if "purpose" in op:
        session["purpose"] = _text(op["purpose"], f"{field}.purpose")
    # Assigned whole, so a restated plan leaves nothing of the one it replaced behind
    # (issue #100). A reduce that restates nothing is lowering the duration of work whose
    # structure did not change: the plan stays, and so does the sentence rendered from it,
    # which is the same sentence because the same plan renders it.
    if "plan" in op:
        session["plan"] = _object(op["plan"], f"{field}.plan")
    _coach_note(session, op, field)


def _replace(session: dict[str, Any], op: dict[str, Any], field: str) -> None:
    sport = (
        _enum(op["sport"], f"{field}.sport", SPORTS) if "sport" in op else session.get("sport")
    )
    session["sport"] = sport
    session["purpose"] = _text(op.get("purpose"), f"{field}.purpose")
    session["adaptation"] = _enum(op.get("adaptation"), f"{field}.adaptation", ADAPTATIONS)
    session["cost"] = _enum(op.get("cost"), f"{field}.cost", COSTS)
    session["planned_minutes"] = _minutes(
        op.get("planned_minutes"), f"{field}.planned_minutes"
    )
    # Required, and assigned whole. A replace re-decides what the session executes, so
    # nothing structural survives from the session it replaced -- the case issue #100 had
    # to pop field by field, and which the sport moving no longer changes.
    session["plan"] = _object(op.get("plan"), f"{field}.plan")
    if "measures" in op:
        # Null clears it: a replace that rewrites the session out of the measurement's
        # specification should be able to say it is no longer the comparison, rather than
        # leaving a link to a reference it no longer repeats.
        if op["measures"] is None:
            session.pop("measures", None)
        else:
            session["measures"] = _text(op["measures"], f"{field}.measures")
    if "scheduled_date" in op:
        session["scheduled_date"] = _date(op["scheduled_date"], f"{field}.scheduled_date")
    if "body_stress" in op:
        session["body_stress"] = _enum(
            op["body_stress"], f"{field}.body_stress", BODY_STRESS
        )
    if "priority" in op:
        session["priority"] = _enum(op["priority"], f"{field}.priority", PRIORITIES)
    if "time_window" in op:
        session["time_window"] = (
            None
            if op["time_window"] is None
            else _text(op["time_window"], f"{field}.time_window")
        )
    if "fallback" in op:
        session["fallback"] = _fallback(op["fallback"], f"{field}.fallback")
    # Kept unless restated, like time_window, body_stress and priority above -- and null
    # is how a replace says the sentence no longer applies to what this session now is.
    _coach_note(session, op, field)
    session["match_status"] = "replaced"


def _reset_stale_delivery(before: dict[str, Any] | None, session: dict[str, Any]) -> None:
    """Withdraw a delivery observation the change just invalidated.

    A session already accepted by Intervals whose executable content moves no longer
    describes what was delivered. Validation demands that the observation be reset rather
    than carried forward, and doing it here is what keeps that from being one more thing
    the model has to remember.

    Resetting the observation does not remove the event: Intervals still shows the athlete
    the workout they were previously told to follow, on its original date, until something
    replaces or withdraws it (issue #113). So the id it was delivered under is kept here
    rather than dropped -- a plan that says nothing about an event it created cannot say
    the calendar matches.
    """
    if before is None or (before.get("execution") or {}).get("delivery_state") != "intervals_accepted":
        return
    if delivery_session_content(before) == delivery_session_content(session):
        return
    session["execution"] = {
        **session.get("execution", {}),
        "external_id": None,
        "delivery_state": "not_published",
        "superseded_external_id": (before.get("execution") or {}).get("external_id"),
    }


def _apply_sessions(
    before: dict[str, Any],
    after: dict[str, Any],
    value: Any,
    language: str,
) -> list[dict[str, str]]:
    operations = _array(value, "change_request.sessions")
    sessions = after["week"]["sessions"]
    before_by_id = _sessions_by_id(before)
    records: list[dict[str, str]] = []
    touched: set[str] = set()

    for index, raw in enumerate(operations):
        field = f"change_request.sessions[{index}]"
        op = _object(raw, field)
        operation = _enum(op.get("operation"), f"{field}.operation", set(OPERATIONS))
        required, optional = _OPERATION_FIELDS[operation]
        _keys(op, field, ("operation", *required), optional)

        if operation == "add":
            taken = {str(item.get("session_id")) for item in sessions if isinstance(item, dict)}
            session = _added_session(op, field, taken, language)
            _insert_in_date_order(sessions, session)
        else:
            session_id = _text(op.get("session_id"), f"{field}.session_id")
            session = next(
                (
                    item
                    for item in sessions
                    if isinstance(item, dict) and item.get("session_id") == session_id
                ),
                None,
            )
            if session is None:
                raise ChangeRequestError(
                    f"{field} names {session_id}, which is not a session in the current week"
                )
            if operation == "keep":
                _keep(session, op, field)
            elif operation == "move":
                _move(session, op, field)
            elif operation == "reduce":
                _reduce(session, op, field)
            elif operation == "replace":
                _replace(session, op, field)

        session_id = str(session["session_id"])
        if session_id in touched:
            raise ChangeRequestError(
                f"{field} changes {session_id} again; give each session one operation"
            )
        touched.add(session_id)

        if operation == "reduce" and session.get("plan", {}).get("kind") == "time_axis":
            # A time axis *is* the duration, so lowering planned_minutes without saying
            # what the axis now holds leaves the structure stale -- and the stale one is
            # still what delivery would send to the watch. A movement list and an
            # unstructured session carry no duration to contradict, so neither is asked
            # for a plan it did not change. Moving a session to another day changes
            # nothing it executes, and add and replace already require their plan.
            if "plan" not in op:
                raise ChangeRequestError(
                    f"{field} shortens {session_id}, whose plan lays work out along time, "
                    "so it needs the plan that now matches it"
                )
        # A bare keep changes nothing, so it is the one operation that may skip
        # bookkeeping entirely -- unless it reworded purpose or moved the coach note, both
        # of which are delivered content (delivery_content.delivery_session_content) and
        # so still have to be checked against an already-delivered observation exactly
        # like any other operation.
        if operation != "keep" or "purpose" in op or "coach_note" in op:
            _bookkeeping(
                session,
                before_by_id.get(session_id),
                # Every operation that can hand a session new executable content has to
                # re-derive whether it publishes -- including reduce, which the check
                # above requires to bring a matching plan. Only move is exempt: a
                # different day executes the same thing. A keep that reworded purpose
                # needs no recompute either: publish_supported for strength depends only
                # on purpose being non-empty, which _text already guarantees on the way in.
                recompute_publish=operation in {"add", "replace", "reduce"},
                language=language,
            )
        records.append({"operation": operation, "session_id": session_id})
    return records


def _publish_supported(session: dict[str, Any]) -> bool:
    """Whether delivery has something it could actually send for this session.

    Sport decides *how* a session is delivered, and both ways are implemented: a running
    session is written as the workout its time_axis plan describes, and a strength session
    as a calendar entry titled with the day's purpose -- which is why ``delivery`` refuses
    one whose purpose cannot title it. Reading this flag as "a run along a time axis"
    described only the first way, so every strength session a writer touched came out
    unpublishable and the delivery boundary then refused a sport the product had already
    been putting on the athlete's calendar.
    """
    sport = session.get("sport")
    if sport == "running":
        plan = session.get("plan")
        return isinstance(plan, dict) and plan.get("kind") == "time_axis"
    if sport == "strength":
        purpose = session.get("purpose")
        return isinstance(purpose, str) and bool(purpose.strip())
    return False


def _bookkeeping(
    session: dict[str, Any],
    before: dict[str, Any] | None,
    *,
    recompute_publish: bool,
    language: str,
) -> None:
    """Derive what a session's own content already decides, then fix delivery state."""
    session["hard"] = session.get("cost") == "hard"
    # The sentence is a rendering of the plan, so it is re-derived after every operation
    # rather than carried: no operation may leave a session describing itself as what it
    # used to be.
    session["prescription"] = render_prescription(session.get("plan"), language)
    if recompute_publish:
        # Whether the product can publish a session is a fact about what delivery could
        # send for it, not a flag anybody gets to assert.
        session["execution"]["publish_supported"] = _publish_supported(session)
    _reset_stale_delivery(before, session)


def _insert_in_date_order(sessions: list[Any], session: dict[str, Any]) -> None:
    date = session["scheduled_date"]
    for index, existing in enumerate(sessions):
        if isinstance(existing, dict) and str(existing.get("scheduled_date", "")) > date:
            sessions.insert(index, session)
            return
    sessions.append(session)


# --------------------------------------------------------------------------------------
# What changed, said in exact values
# --------------------------------------------------------------------------------------


def _session_line(session: dict[str, Any]) -> str:
    parts = [session.get("session_id"), session.get("scheduled_date"), session.get("sport")]
    minutes = session.get("planned_minutes")
    if isinstance(minutes, int) and not isinstance(minutes, bool):
        parts.append(f"{minutes}min")
    parts.append(session.get("cost"))
    line = " ".join(str(part) for part in parts if part)
    prescription = session.get("prescription")
    if isinstance(prescription, str) and prescription.strip():
        return f"{line}: {prescription.strip()}"
    return line


def _cycle_line(plan: dict[str, Any]) -> str:
    cycle = plan.get("cycle") or {}
    return (
        f"cycle: {cycle.get('primary_adaptation')} "
        f"{cycle.get('start')}..{cycle.get('end')}"
    )


def _week_line(plan: dict[str, Any]) -> str:
    week = plan.get("week") or {}
    return f"week from {week.get('start')}: {week.get('intent')}"


def _week_header(plan: dict[str, Any]) -> dict[str, Any]:
    week = plan.get("week") or {}
    return {field: week.get(field) for field in _WEEK_FIELDS}


def _strength_load_phrase(load: dict[str, Any] | None) -> str:
    if load is None:
        return "not set"
    parts: list[str] = []
    if load.get("load_kg") is not None:
        parts.append(f"{load['load_kg']}kg")
    if load.get("assist_kg") is not None:
        parts.append(f"assist {load['assist_kg']}kg")
    if load.get("scheme"):
        parts.append(str(load["scheme"]))
    return " ".join(parts) if parts else "no figure"


def _baseline_lines(
    before_baseline: dict[str, Any], after_baseline: dict[str, Any]
) -> tuple[str, str]:
    """Both sides of a baseline change, changed fields only.

    The whole baseline would bury the one anchor that moved under six that did not;
    the narrative exists so the athlete can see exactly what the coach re-measured.
    """
    before_parts: list[str] = []
    after_parts: list[str] = []
    scalar_fields = ATHLETE_BASELINE_INTEGER_FIELDS + ATHLETE_BASELINE_NUMBER_FIELDS
    for name in scalar_fields:
        if before_baseline.get(name) != after_baseline.get(name):
            before_parts.append(f"{name}: {before_baseline.get(name)}")
            after_parts.append(f"{name}: {after_baseline.get(name)}")
    before_loads = [
        load for load in before_baseline.get("strength_loads") or [] if isinstance(load, dict)
    ]
    for load in after_baseline.get("strength_loads") or []:
        if not isinstance(load, dict):
            continue
        previous = anchoring_baseline(load.get("exercise"), before_loads)
        if previous != load:
            name = load.get("display_name") or load.get("exercise")
            before_parts.append(f"{name}: {_strength_load_phrase(previous)}")
            after_parts.append(f"{name}: {_strength_load_phrase(load)}")
    return (
        f"baseline {' | '.join(before_parts)}",
        f"baseline {' | '.join(after_parts)}",
    )


def _change_narrative(
    before: dict[str, Any],
    after: dict[str, Any],
    changed_ids: list[str],
    rolled_out: list[dict[str, Any]],
) -> tuple[str, str]:
    """The two sides of the change, in the plan's own values.

    Derived rather than asked for: a coach describing their own edit from memory is
    exactly the reconstruction the athlete should not have to trust.
    """
    before_by_id, after_by_id = _sessions_by_id(before), _sessions_by_id(after)
    before_parts: list[str] = []
    after_parts: list[str] = []
    if before.get("goal") != after.get("goal"):
        before_parts.append(f"goal: {(before.get('goal') or {}).get('outcome')}")
        after_parts.append(f"goal: {(after.get('goal') or {}).get('outcome')}")
    if before.get("cycle") != after.get("cycle"):
        before_parts.append(_cycle_line(before))
        after_parts.append(_cycle_line(after))
    if before.get("athlete_baseline") != after.get("athlete_baseline"):
        baseline_before, baseline_after = _baseline_lines(
            before.get("athlete_baseline") or {}, after.get("athlete_baseline") or {}
        )
        before_parts.append(baseline_before)
        after_parts.append(baseline_after)
    if _week_header(before) != _week_header(after):
        before_parts.append(_week_line(before))
        after_parts.append(_week_line(after))
    for session_id in changed_ids:
        before_session = before_by_id.get(session_id)
        before_parts.append(
            _session_line(before_session) if before_session else f"{session_id}: not in the week"
        )
        after_parts.append(_session_line(after_by_id[session_id]))
    # A rolled-out session is in `before` and in no `changed_ids`, because that list can
    # only name what `after` still holds. Left out, the event would describe a week
    # rolling forward as though nothing had left it.
    for session in rolled_out:
        before_parts.append(_session_line(session))
        after_parts.append(f"{session.get('session_id')}: rolled out of the week")
    if not before_parts:
        unchanged = f"{before.get('plan_id')} v{before.get('version')} unchanged"
        return unchanged, unchanged
    return " | ".join(before_parts), " | ".join(after_parts)


def _session_view(session: dict[str, Any] | None) -> dict[str, Any] | None:
    if session is None:
        return None
    plan = session.get("plan") if isinstance(session.get("plan"), dict) else {}
    return {
        # `prescription` is already in _PREVIEW_SESSION_FIELDS, and it is the rendering of
        # the very plan below -- every movement, set, rep and load the athlete is about to
        # adopt. Issue #100 had to put the structured list beside the sentence because the
        # two were authored separately and could disagree; here one generates the other, so
        # what is confirmed is the record and not a paraphrase of it.
        **{field: session.get(field) for field in _PREVIEW_SESSION_FIELDS},
        "plan_kind": plan.get("kind"),
        "workout_name": plan.get("name"),
        "delivery_state": (session.get("execution") or {}).get("delivery_state"),
    }


def _changed_fields(before: dict[str, Any] | None, after: dict[str, Any]) -> list[str]:
    if before is None:
        return []
    return sorted(
        field
        for field in set(before) | set(after)
        if _canonical(before.get(field)) != _canonical(after.get(field))
    )


def _weekly_minutes(plan: dict[str, Any]) -> int | None:
    values = [
        session.get("planned_minutes") for session in ((plan.get("week") or {}).get("sessions") or [])
        if isinstance(session, dict)
    ]
    if any(isinstance(value, bool) or not isinstance(value, int) for value in values):
        return None
    return sum(values)


def _hard_count(plan: dict[str, Any]) -> int:
    return sum(
        session.get("hard") is True
        for session in ((plan.get("week") or {}).get("sessions") or [])
        if isinstance(session, dict)
    )


def _preview(
    before: dict[str, Any],
    after: dict[str, Any],
    records: list[dict[str, str]],
    rolled_out: list[dict[str, Any]],
    *,
    material_change: bool,
) -> dict[str, Any]:
    """The before/after an athlete confirms: values, not field names.

    Every number here is copied out of the two PlanStates the server itself just built, so
    confirming the preview and confirming the change are the same act.
    """
    before_by_id, after_by_id = _sessions_by_id(before), _sessions_by_id(after)
    return {
        "material_change": material_change,
        "base_version": before.get("version"),
        "resulting_version": after.get("version"),
        "goal": (
            None
            if before.get("goal") == after.get("goal")
            else {"before": before.get("goal"), "after": after.get("goal")}
        ),
        "cycle": (
            None
            if before.get("cycle") == after.get("cycle")
            else {"before": before.get("cycle"), "after": after.get("cycle")}
        ),
        "athlete_baseline": (
            None
            if before.get("athlete_baseline") == after.get("athlete_baseline")
            else {
                "before": before.get("athlete_baseline"),
                "after": after.get("athlete_baseline"),
            }
        ),
        "week": (
            None
            if _week_header(before) == _week_header(after)
            else {"before": _week_header(before), "after": _week_header(after)}
        ),
        "sessions": [
            {
                "operation": record["operation"],
                "session_id": record["session_id"],
                "changed_fields": _changed_fields(
                    before_by_id.get(record["session_id"]), after_by_id[record["session_id"]]
                ),
                "before": _session_view(before_by_id.get(record["session_id"])),
                "after": _session_view(after_by_id[record["session_id"]]),
            }
            for record in records
        ],
        # The sessions the week moved past. They are not an operation and never appear in
        # `sessions` above, so without this the only trace of a whole week retiring is
        # `weekly_planned_minutes` falling -- a total the athlete would have to infer a
        # roll from. Their last written state travels with them, including what was
        # delivered, because that is what is being handed to history.
        "rolled_out": [_session_view(session) for session in rolled_out],
        "weekly_planned_minutes": {
            "before": _weekly_minutes(before),
            "after": _weekly_minutes(after),
        },
        "hard_sessions": {"before": _hard_count(before), "after": _hard_count(after)},
    }


# --------------------------------------------------------------------------------------
# The DecisionEvent
# --------------------------------------------------------------------------------------


def _decision_event(
    before: dict[str, Any],
    after: dict[str, Any],
    *,
    request: dict[str, Any],
    coaching: dict[str, Any],
    context: dict[str, Any],
    changed_ids: list[str],
    rolled_out: list[dict[str, Any]],
    material_change: bool,
    issued_at: dt.datetime,
) -> dict[str, Any]:
    """Build the event this projection represents. Every mechanical field is derived.

    Mode follows what actually moved rather than what the request called itself: a change
    that leaves the goal and the 28-day cycle alone is a week review, and saying so is
    what lets validation hold the goal and cycle still.
    """
    goal_or_cycle_moved = (
        before.get("goal") != after.get("goal") or before.get("cycle") != after.get("cycle")
    )
    reason_codes = list(coaching["reason_codes"])
    if not material_change and "plan_kept_no_material_change" not in reason_codes:
        reason_codes.append("plan_kept_no_material_change")

    inputs_used = [f"plan_state:{before['plan_id']}@v{before['version']}"]
    context_id = context.get("context_id")
    if isinstance(context_id, str) and context_id.strip():
        inputs_used.append(f"coach_context:{context_id}")
    inputs_used.append("coach_change_request")

    context_unknowns = context.get("unknowns")
    unknowns = sorted(
        set(coaching["unknowns"])
        | {item for item in (context_unknowns or []) if isinstance(item, str)}
    )
    change_before, change_after = _change_narrative(before, after, changed_ids, rolled_out)
    identity = canonical_hash(
        {
            "plan_id": before["plan_id"],
            "base_version": before["version"],
            "change_request": request,
            "issued_at": _utc_iso(issued_at),
        }
    )
    return {
        "schema_version": DECISION_EVENT_SCHEMA_VERSION,
        "event_id": f"decision-{identity[:24]}",
        "mode": "review_cycle" if goal_or_cycle_moved else "review_week",
        "plan_id": before["plan_id"],
        "plan_version_before": before["version"],
        "plan_version_after": after["version"],
        "action": "adjust" if material_change else "keep",
        "session_id": changed_ids[0] if len(changed_ids) == 1 else None,
        "inputs_used": inputs_used,
        "evidence": coaching["evidence"],
        "unknowns": unknowns,
        "reason_codes": reason_codes,
        "change": {
            "before": change_before,
            "after": change_after,
            "summary": coaching["summary"],
        },
        "goal_effect": coaching["goal_effect"],
        "next_review_condition": coaching["next_review_condition"],
        "created_at": _utc_iso(issued_at),
    }


# --------------------------------------------------------------------------------------
# The projection
# --------------------------------------------------------------------------------------


def project_change_request(
    before: dict[str, Any],
    change_request: Any,
    *,
    context: dict[str, Any],
    issued_at: dt.datetime,
    language: str = DEFAULT_LANGUAGE,
) -> dict[str, Any]:
    """Turn one coaching change request into a candidate PlanState and DecisionEvent.

    Pure and total: the same current plan, request, instant and language always produce
    the same two artifacts, byte for byte, which is what lets a confirmation bind them
    without either being stored or round-tripped through the agent.

    ``language`` reaches only the sessions this request actually touches. A session the
    change leaves alone keeps the sentence it was written with, so an athlete who switches
    language does not silently rewrite the week they are already training -- their next
    change is written the new way, and the week converges as it is edited.
    """
    request = _object(change_request, "change_request")
    _keys(request, "change_request", _REQUIRED_FIELDS, _OPTIONAL_FIELDS)
    coaching = {
        "summary": _text(request.get("summary"), "change_request.summary"),
        "reason_codes": _reason_codes(request.get("reason_codes")),
        "evidence": _evidence(request.get("evidence")),
        "goal_effect": _goal_effect(request.get("goal_effect")),
        "next_review_condition": _text(
            request.get("next_review_condition"), "change_request.next_review_condition"
        ),
        "unknowns": _text_array(request.get("unknowns") or [], "change_request.unknowns"),
    }

    after = copy.deepcopy(before)
    _apply_goal(after, request.get("goal"))
    _apply_cycle(after, request.get("cycle"))
    _apply_athlete_baseline(after, request.get("athlete_baseline"))
    _apply_week(after, request.get("week"))
    records = _apply_sessions(before, after, request.get("sessions") or [], language)
    rolled_out = _roll_past_sessions(before, after, records)

    # The store's own rule, applied to the same comparison it makes: a version is spent
    # only when the plan actually moved.
    material_change = canonical_hash(before) != canonical_hash(after)
    after["version"] = before["version"] + (1 if material_change else 0)

    before_by_id, after_by_id = _sessions_by_id(before), _sessions_by_id(after)
    changed_ids = [
        session_id
        for session_id in after_by_id
        if _canonical(before_by_id.get(session_id)) != _canonical(after_by_id[session_id])
    ]
    event = _decision_event(
        before,
        after,
        request=request,
        coaching=coaching,
        context=context if isinstance(context, dict) else {},
        changed_ids=changed_ids,
        rolled_out=rolled_out,
        material_change=material_change,
        issued_at=issued_at,
    )
    return {
        "after_plan": after,
        "decision_event": event,
        "preview": _preview(
            before, after, records, rolled_out, material_change=material_change
        ),
        "material_change": material_change,
    }
