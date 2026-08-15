"""Project one coaching initialization request onto a first PlanState.

``plan_change`` fixed the write side for an athlete who already has a plan: the model
sends coaching judgment, deterministic code derives everything mechanical. The first plan
was still asked for whole -- one complete version 1 PlanState, opaque to the contract
carrying it -- so the very first thing a new athlete's coach had to do was reconstruct an
internal schema from an ordinary onboarding conversation. That is the same failure the
change path had, on the one journey where there is no existing plan to fall back to.

So the request here carries coaching judgment and athlete facts only:

- what the athlete is training for, and how that will be measured;
- where the 28 days point: primary and maintenance adaptation, the evidence the cycle
  should produce, what would adjust it, what would stop it;
- what the first week is for, and the sessions in it;
- when the athlete can train and with what equipment;
- which baselines they actually support -- and, just as importantly, which they do not;
- why: a summary, the evidence behind it, and what could not be established.

Everything mechanical is derived below: schema version, plan id, plan version, status,
the cycle's end date, the week start, session ids, ``hard``, ``publish_supported``,
delivery state and ``match_status``. ``validate_plan_state`` remains the only authority
on whether the result may be stored.

Two things this module deliberately does not do. It does not plan: there is no default
week, no template session, and a request naming no session is refused rather than filled
in. And it does not invent precision: a baseline the athlete did not give stays null and
is named in ``unknowns``, never rounded to zero or to a population figure (AGENTS.md 3).

The request-shape helpers and the refusal type are ``plan_change``'s. Both modules refuse
a malformed coaching request, and they have to refuse it the same way -- two copies of
that vocabulary would drift into two contracts.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from .plan_change import (
    ChangeRequestError,
    _array,
    _date,
    _enum,
    _fallback,
    _hard_count,
    _insert_in_date_order,
    _keys,
    _minutes,
    _new_session_id,
    _object,
    _publish_supported,
    _text,
    _text_array,
    _utc_iso,
    _weekly_minutes,
)
from .prescription import render_prescription
from .store import canonical_hash
from .validation import (
    ADAPTATIONS,
    ATHLETE_BASELINE_INTEGER_FIELDS,
    ATHLETE_BASELINE_NUMBER_FIELDS,
    BODY_STRESS,
    COSTS,
    PLAN_STATE_SCHEMA_VERSION,
    PRIORITIES,
    SPORTS,
)


# The cycle is 28 days inclusive, which makes its end date arithmetic rather than
# judgment. The model says when the block starts; the calendar says when it ends.
CYCLE_DAYS = 28

_REQUIRED_FIELDS = ("goal", "cycle", "week_intent", "sessions", "summary", "evidence")
_OPTIONAL_FIELDS = ("availability", "baselines", "unknowns")

# No ``end``: see CYCLE_DAYS. ``maintenance_adaptation`` is optional and may be null --
# a block that maintains nothing is a real answer, not a missing one.
_CYCLE_REQUIRED = (
    "start",
    "primary_adaptation",
    "planned_evidence",
    "adjust_conditions",
    "stop_conditions",
)
_CYCLE_OPTIONAL = ("maintenance_adaptation",)

# No ``prescription``: it is rendered from ``plan`` (issue #93). ``plan`` is required on
# every session, ``unstructured`` included -- which execution model a session is planned
# under is a fact about the session, and a first plan states it like any other.
_SESSION_REQUIRED = (
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
)
_SESSION_OPTIONAL = ("time_window",)

_AVAILABILITY_FIELDS = ("days", "equipment")

# Split by type rather than listed once, because "not measured" has to survive as null
# through a checker that would otherwise be happy to read 0 as a number. The split
# itself lives in `validation` so the hosted baseline change parses the same way.
_BASELINE_INTEGERS = ATHLETE_BASELINE_INTEGER_FIELDS
_BASELINE_NUMBERS = ATHLETE_BASELINE_NUMBER_FIELDS
_STRENGTH_LOAD_OPTIONAL = ("load_kg", "assist_kg", "scheme", "display_name")

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
)


# --------------------------------------------------------------------------------------
# The athlete's own facts
# --------------------------------------------------------------------------------------


def _optional_integer(value: Any, field: str, *, minimum: int) -> int | None:
    """A measured whole number, or null.

    Null is the answer for an anchor nobody has measured, and it has to stay null: a
    threshold pace of 0 is not an unknown threshold pace, it is a claim (AGENTS.md 3).
    """
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ChangeRequestError(
            f"{field} must be an integer >= {minimum}, or null when it is not measured"
        )
    return value


def _optional_number(value: Any, field: str, *, minimum: float) -> int | float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < minimum:
        raise ChangeRequestError(
            f"{field} must be a number >= {minimum}, or null when it is not measured"
        )
    return value


def _strength_loads(value: Any, field: str) -> list[dict[str, Any]]:
    """The lifts the athlete has an actual figure for.

    Every entry carries all four baseline keys, absent ones filled with null here rather
    than by the model: which column a lift measures is a property of the lift -- an
    assisted pull-up records ``assist_kg`` and leaves ``load_kg`` empty -- and asking for
    both back makes a missing one indistinguishable from a forgotten one.
    """
    loads = []
    for index, raw in enumerate(_array(value, field)):
        item_field = f"{field}[{index}]"
        item = _object(raw, item_field)
        _keys(item, item_field, ("exercise",), _STRENGTH_LOAD_OPTIONAL)
        load: dict[str, Any] = {
            "exercise": _text(item.get("exercise"), f"{item_field}.exercise"),
            "load_kg": _optional_number(item.get("load_kg"), f"{item_field}.load_kg", minimum=0),
            "assist_kg": _optional_number(
                item.get("assist_kg"), f"{item_field}.assist_kg", minimum=0
            ),
            "scheme": (
                None
                if item.get("scheme") is None
                else _text(item.get("scheme"), f"{item_field}.scheme")
            ),
        }
        if item.get("display_name") is not None:
            load["display_name"] = _text(
                item.get("display_name"), f"{item_field}.display_name"
            )
        loads.append(load)
    return loads


def _athlete_baseline(value: Any) -> dict[str, Any]:
    """Build the plan's baseline from what the athlete supports, and nothing else.

    Always emitted, even when every field is null. A plan carrying no baseline at all
    means it predates the field; a plan carrying an all-null baseline means the athlete
    was asked and the answer is not known yet. The two are different facts and the
    validator reads them differently.
    """
    field = "initialization_request.baselines"
    baselines = {} if value is None else _object(value, field)
    _keys(baselines, field, (), _BASELINE_INTEGERS + _BASELINE_NUMBERS + ("strength_loads",))
    baseline: dict[str, Any] = {
        name: _optional_integer(baselines.get(name), f"{field}.{name}", minimum=1)
        for name in _BASELINE_INTEGERS
    }
    for name in _BASELINE_NUMBERS:
        baseline[name] = _optional_number(baselines.get(name), f"{field}.{name}", minimum=0)
    baseline["strength_loads"] = _strength_loads(
        baselines.get("strength_loads") or [], f"{field}.strength_loads"
    )
    return baseline


def _measured(load: dict[str, Any]) -> bool:
    return any(
        isinstance(load.get(key), (int, float)) and not isinstance(load.get(key), bool)
        for key in ("load_kg", "assist_kg")
    )


def _baseline_unknowns(baseline: dict[str, Any]) -> list[str]:
    """Name every anchor the plan does not stand on.

    Derived rather than asked for. An unmeasured anchor is a fact about the baseline
    object, and a coach who forgot to mention it would leave the athlete confirming a
    plan without knowing which of its numbers are guesses.
    """
    unknowns = [
        f"athlete_baseline.{name} is not measured"
        for name in (*_BASELINE_INTEGERS, *_BASELINE_NUMBERS)
        if baseline.get(name) is None
    ]
    if not any(_measured(load) for load in baseline["strength_loads"]):
        unknowns.append("athlete_baseline.strength_loads has no measured lift")
    return unknowns


def _availability(value: Any) -> dict[str, list[str]] | None:
    """When the athlete can train and with what, echoed back for them to check.

    PlanState has no field for either, and inventing one would be a schema change in
    service of a fact the sessions already express through their days, time windows and
    prescriptions. It is carried into the preview instead, where mishearing "Tuesday and
    Thursday" costs one correction rather than 28 days of training.
    """
    if value is None:
        return None
    field = "initialization_request.availability"
    availability = _object(value, field)
    _keys(availability, field, (), _AVAILABILITY_FIELDS)
    return {
        name: _text_array(availability.get(name) or [], f"{field}.{name}")
        for name in _AVAILABILITY_FIELDS
    }


# --------------------------------------------------------------------------------------
# The coaching half: goal, cycle, and why
# --------------------------------------------------------------------------------------


def _goal(value: Any) -> dict[str, str]:
    field = "initialization_request.goal"
    goal = _object(value, field)
    _keys(goal, field, ("outcome", "measurement_protocol"))
    return {
        "outcome": _text(goal.get("outcome"), f"{field}.outcome"),
        "measurement_protocol": _text(
            goal.get("measurement_protocol"), f"{field}.measurement_protocol"
        ),
    }


def _cycle(value: Any) -> dict[str, Any]:
    field = "initialization_request.cycle"
    cycle = _object(value, field)
    _keys(cycle, field, _CYCLE_REQUIRED, _CYCLE_OPTIONAL)
    start = _date(cycle.get("start"), f"{field}.start")
    maintenance = cycle.get("maintenance_adaptation")
    return {
        "start": start,
        "end": (
            dt.date.fromisoformat(start) + dt.timedelta(days=CYCLE_DAYS - 1)
        ).isoformat(),
        "primary_adaptation": _enum(
            cycle.get("primary_adaptation"), f"{field}.primary_adaptation", ADAPTATIONS
        ),
        "maintenance_adaptation": (
            None
            if maintenance is None
            else _enum(maintenance, f"{field}.maintenance_adaptation", ADAPTATIONS)
        ),
        **{
            name: _text_array(cycle.get(name), f"{field}.{name}", minimum=1)
            for name in ("planned_evidence", "adjust_conditions", "stop_conditions")
        },
    }


def _evidence(value: Any) -> list[dict[str, str]]:
    field = "initialization_request.evidence"
    items = _array(value, field)
    if not items:
        raise ChangeRequestError(f"{field} must cite at least one thing the athlete told you")
    evidence = []
    for index, raw in enumerate(items):
        item_field = f"{field}[{index}]"
        item = _object(raw, item_field)
        _keys(item, item_field, ("field", "observation"))
        evidence.append(
            {
                "field": _text(item.get("field"), f"{item_field}.field"),
                "observation": _text(item.get("observation"), f"{item_field}.observation"),
            }
        )
    return evidence


# --------------------------------------------------------------------------------------
# The first week's sessions
# --------------------------------------------------------------------------------------


def _session(raw: Any, field: str, taken: set[str]) -> dict[str, Any]:
    session_request = _object(raw, field)
    _keys(session_request, field, _SESSION_REQUIRED, _SESSION_OPTIONAL)
    sport = _enum(session_request.get("sport"), f"{field}.sport", SPORTS)
    scheduled_date = _date(session_request.get("scheduled_date"), f"{field}.scheduled_date")
    cost = _enum(session_request.get("cost"), f"{field}.cost", COSTS)
    session: dict[str, Any] = {
        "session_id": _new_session_id(sport, scheduled_date, taken),
        "sport": sport,
        "scheduled_date": scheduled_date,
        "time_window": (
            None
            if session_request.get("time_window") is None
            else _text(session_request.get("time_window"), f"{field}.time_window")
        ),
        "purpose": _text(session_request.get("purpose"), f"{field}.purpose"),
        "adaptation": _enum(
            session_request.get("adaptation"), f"{field}.adaptation", ADAPTATIONS
        ),
        "body_stress": _enum(
            session_request.get("body_stress"), f"{field}.body_stress", BODY_STRESS
        ),
        "cost": cost,
        "priority": _enum(session_request.get("priority"), f"{field}.priority", PRIORITIES),
        "planned_minutes": _minutes(
            session_request.get("planned_minutes"), f"{field}.planned_minutes"
        ),
        # Derived, never claimed: how hard a session is follows the cost it was given.
        "hard": cost == "hard",
        "plan": _object(session_request.get("plan"), f"{field}.plan"),
        "fallback": _fallback(session_request.get("fallback"), f"{field}.fallback"),
        "execution": {
            "publish_supported": False,
            "external_id": None,
            "delivery_state": "not_published",
        },
        "match_status": "planned",
    }
    # Rendered from the plan, never taken from the request: the athlete's first plan is
    # held to the same rule as every later one -- prose is an output.
    session["prescription"] = render_prescription(session["plan"])
    # A session publishes exactly when delivery could send it: the workout a run's
    # time_axis plan describes, or the purpose that titles a strength calendar entry.
    # The flag is a fact about the session, not something a request may assert.
    session["execution"]["publish_supported"] = _publish_supported(session)
    return session


def _sessions(value: Any) -> list[dict[str, Any]]:
    field = "initialization_request.sessions"
    operations = _array(value, field)
    if not operations:
        raise ChangeRequestError(
            f"{field} must contain the first week's sessions; there is no default week"
        )
    sessions: list[dict[str, Any]] = []
    for index, raw in enumerate(operations):
        taken = {str(item["session_id"]) for item in sessions}
        _insert_in_date_order(sessions, _session(raw, f"{field}[{index}]", taken))
    return sessions


# --------------------------------------------------------------------------------------
# What the athlete confirms, in exact values
# --------------------------------------------------------------------------------------


def _session_view(session: dict[str, Any]) -> dict[str, Any]:
    return {
        **{name: session.get(name) for name in _PREVIEW_SESSION_FIELDS},
        "plan": session.get("plan"),
    }


def _preview(
    plan: dict[str, Any],
    *,
    summary: str,
    evidence: list[dict[str, str]],
    unknowns: list[str],
    availability: dict[str, list[str]] | None,
) -> dict[str, Any]:
    """The first plan an athlete confirms: values, not field names.

    Every number is copied out of the PlanState the server itself just built, so
    confirming the preview and confirming the plan are the same act. The facts it was
    built from travel with it -- the athlete is the only one who can catch a misheard
    training day or a strength load that was never theirs.
    """
    cycle, week = plan["cycle"], plan["week"]
    return {
        "plan_id": plan["plan_id"],
        "plan_version": plan["version"],
        "goal": plan["goal"],
        "cycle": {
            name: cycle[name]
            for name in ("start", "end", "primary_adaptation", "maintenance_adaptation")
        },
        "week": {"start": week["start"], "intent": week["intent"]},
        "sessions": [_session_view(session) for session in week["sessions"]],
        "weekly_planned_minutes": _weekly_minutes(plan),
        "hard_sessions": _hard_count(plan),
        "athlete_baseline": plan["athlete_baseline"],
        "availability": availability,
        "summary": summary,
        "evidence": evidence,
        "unknowns": unknowns,
    }


# --------------------------------------------------------------------------------------
# The projection
# --------------------------------------------------------------------------------------


def project_initialization_request(
    initialization_request: Any,
    *,
    issued_at: dt.datetime,
) -> dict[str, Any]:
    """Turn one coaching initialization request into a candidate first PlanState.

    Pure and total: the same request and instant always produce the same plan, byte for
    byte, which is what lets one confirmation bind a plan the agent never holds.
    """
    request = _object(initialization_request, "initialization_request")
    _keys(request, "initialization_request", _REQUIRED_FIELDS, _OPTIONAL_FIELDS)

    summary = _text(request.get("summary"), "initialization_request.summary")
    evidence = _evidence(request.get("evidence"))
    availability = _availability(request.get("availability"))
    baseline = _athlete_baseline(request.get("baselines"))
    cycle = _cycle(request.get("cycle"))

    plan = {
        "schema_version": PLAN_STATE_SCHEMA_VERSION,
        # Derived from what this plan is, not from a counter or a clock, so preparing and
        # applying the same request name the same plan both times.
        "plan_id": "plan-"
        + canonical_hash(
            {"initialization_request": request, "issued_at": _utc_iso(issued_at)}
        )[:24],
        "version": 1,
        "status": "active",
        "goal": _goal(request.get("goal")),
        "cycle": cycle,
        "week": {
            # The first week of a 28-day block starts when the block does.
            "start": cycle["start"],
            "intent": _text(request.get("week_intent"), "initialization_request.week_intent"),
            "sessions": _sessions(request.get("sessions")),
        },
        "athlete_baseline": baseline,
    }
    unknowns = sorted(
        set(_text_array(request.get("unknowns") or [], "initialization_request.unknowns"))
        | set(_baseline_unknowns(baseline))
    )
    return {
        "plan": plan,
        "preview": _preview(
            plan,
            summary=summary,
            evidence=evidence,
            unknowns=unknowns,
            availability=availability,
        ),
        "unknowns": unknowns,
    }
