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
    _Errors,
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
    _outlook,
    _publish_supported,
    _text,
    _text_array,
    _utc_iso,
    _weekly_minutes,
)
from .prescription import DEFAULT_LANGUAGE, render_prescription
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
# `week_start` is `change_request.week.start` after translation. A first week's start is
# the cycle's start, so the field is derived here the way `cycle.end` is -- omitted is
# derived, a matching date is accepted, a different date is refused (issue #427).
_OPTIONAL_FIELDS = ("availability", "baselines", "unknowns", "week_start")

# ``maintenance_adaptation`` is optional and may be null -- a block that maintains
# nothing is a real answer, not a missing one.
# ``outlook`` is required here and optional nowhere else that matters: a first plan is
# always at week 1 of a 28-day block, so "what do the next four weeks look like" has an
# answer, and an athlete who has just been asked for their goal and their days should not
# have to ask a second time to see where it goes (issue #61).
_CYCLE_REQUIRED = (
    "start",
    "primary_adaptation",
    "planned_evidence",
    "adjust_conditions",
    "stop_conditions",
    "outlook",
)
# `end` is derived here, not taken -- but it is declared on the one schema a model reads,
# so a model that fills what the schema declares sends it, and refusing it cost every new
# athlete a round trip from the first public commit until issue #427. Accepted and checked
# against the cycle this code is about to build: agreeing costs nothing, and disagreeing is
# worth a sentence rather than a silent overwrite of what the coach said.
_CYCLE_OPTIONAL = ("maintenance_adaptation", "end")

# No ``prescription``: it is rendered from ``plan`` (archived issue #93). ``plan`` is required on
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
# `coach_note` for the same reason as `cycle.end` above (issue #427): declared on the
# schema, described there as "optional on every operation", and refused here -- once per
# session, so a three-session first week paid three separate round trips for one field. A
# first plan's sessions travel to the athlete's calendar exactly as a changed one's do, so
# there was never a reason they could not carry the sentence that goes with them.
_SESSION_OPTIONAL = ("time_window", "coach_note")

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


def _strength_load(raw: Any, item_field: str) -> dict[str, Any]:
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
    return load


def _strength_loads(
    value: Any, field: str, errors: _Errors | None = None
) -> list[dict[str, Any]]:
    """The lifts the athlete has an actual figure for.

    Every entry carries all four baseline keys, absent ones filled with null here rather
    than by the model: which column a lift measures is a property of the lift -- an
    assisted pull-up records ``assist_kg`` and leaves ``load_kg`` empty -- and asking for
    both back makes a missing one indistinguishable from a forgotten one.
    """
    own = errors is None
    if errors is None:
        errors = _Errors()
    items = errors.try_(lambda: _array(value, field), default=[])
    loads: list[dict[str, Any]] = []
    if items is None:
        items = []
    for index, raw in enumerate(items):
        item_field = f"{field}[{index}]"
        load = errors.try_(
            lambda raw=raw, item_field=item_field: _strength_load(raw, item_field)
        )
        if load is not None:
            loads.append(load)
    if own:
        errors.raise_collected()
    return loads


def _athlete_baseline(value: Any, errors: _Errors | None = None) -> dict[str, Any]:
    """Build the plan's baseline from what the athlete supports, and nothing else.

    Always emitted, even when every field is null. A plan carrying no baseline at all
    means it predates the field; a plan carrying an all-null baseline means the athlete
    was asked and the answer is not known yet. The two are different facts and the
    validator reads them differently.
    """
    own = errors is None
    if errors is None:
        errors = _Errors()
    field = "initialization_request.baselines"
    baselines = {} if value is None else errors.try_(lambda: _object(value, field), default={})
    if baselines is None:
        baselines = {}
    else:
        errors.try_(
            lambda: _keys(
                baselines, field, (), _BASELINE_INTEGERS + _BASELINE_NUMBERS + ("strength_loads",)
            )
        )
    baseline: dict[str, Any] = {
        name: errors.try_(
            lambda name=name: _optional_integer(
                baselines.get(name), f"{field}.{name}", minimum=1
            )
        )
        for name in _BASELINE_INTEGERS
    }
    for name in _BASELINE_NUMBERS:
        baseline[name] = errors.try_(
            lambda name=name: _optional_number(
                baselines.get(name), f"{field}.{name}", minimum=0
            )
        )
    baseline["strength_loads"] = _strength_loads(
        baselines.get("strength_loads") or [], f"{field}.strength_loads", errors
    )
    if own:
        errors.raise_collected()
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


def _goal(value: Any, errors: _Errors | None = None) -> dict[str, str]:
    own = errors is None
    if errors is None:
        errors = _Errors()
    field = "initialization_request.goal"
    goal = errors.try_(lambda: _object(value, field))
    if goal is None:
        if own:
            errors.raise_collected()
        return {"outcome": "", "measurement_protocol": ""}
    # No `measurement` here, and that is the contract rather than an omission. Its
    # `reference_session_id` has to name a session that exists, and on a first plan every
    # session is one this same request is creating -- their ids are derived by the server
    # after it is read. A coach could only satisfy the field by constructing an id, which
    # is the one thing every other part of this contract forbids. The measurement is
    # declared at a later decision, when the reference session is on the plan and its id
    # can simply be read off it (issue #13).
    errors.try_(lambda: _keys(goal, field, ("outcome", "measurement_protocol")))
    parsed = {
        "outcome": errors.try_(
            lambda: _text(goal.get("outcome"), f"{field}.outcome"), default=""
        ),
        "measurement_protocol": errors.try_(
            lambda: _text(
                goal.get("measurement_protocol"), f"{field}.measurement_protocol"
            ),
            default="",
        ),
    }
    if own:
        errors.raise_collected()
    return parsed


def _cycle_end_mismatch(field: str, derived: str, stated: str) -> str:
    return (
        f"{field}.end must be {derived} -- a cycle is {CYCLE_DAYS} days from its "
        f"start, so {stated} describes a different one. Send that start instead, "
        "or leave end out and it is derived."
    )


def _cycle(value: Any, errors: _Errors | None = None) -> dict[str, Any] | None:
    own = errors is None
    if errors is None:
        errors = _Errors()
    field = "initialization_request.cycle"
    cycle = errors.try_(lambda: _object(value, field))
    if cycle is None:
        if own:
            errors.raise_collected()
        return None
    errors.try_(lambda: _keys(cycle, field, _CYCLE_REQUIRED, _CYCLE_OPTIONAL))
    start = errors.try_(lambda: _date(cycle.get("start"), f"{field}.start"))
    maintenance = cycle.get("maintenance_adaptation")
    derived_end = (
        None
        if start is None
        else (dt.date.fromisoformat(start) + dt.timedelta(days=CYCLE_DAYS - 1)).isoformat()
    )
    if cycle.get("end") is not None:
        stated = errors.try_(lambda: _date(cycle.get("end"), f"{field}.end"))
        if derived_end is not None and stated is not None and stated != derived_end:
            errors.messages.append(_cycle_end_mismatch(field, derived_end, stated))
    parsed = {
        "start": start or "",
        "end": derived_end or "",
        "primary_adaptation": errors.try_(
            lambda: _enum(
                cycle.get("primary_adaptation"), f"{field}.primary_adaptation", ADAPTATIONS
            ),
            default="",
        ),
        "maintenance_adaptation": (
            None
            if maintenance is None
            else errors.try_(
                lambda: _enum(
                    maintenance, f"{field}.maintenance_adaptation", ADAPTATIONS
                ),
                default="",
            )
        ),
        **{
            name: errors.try_(
                lambda name=name: _text_array(
                    cycle.get(name), f"{field}.{name}", minimum=1
                ),
                default=[],
            )
            for name in ("planned_evidence", "adjust_conditions", "stop_conditions")
        },
        "outlook": (
            _first_cycle_outlook(cycle.get("outlook"), start, errors)
            if "outlook" in cycle
            else []
        ),
    }
    if own:
        errors.raise_collected()
    return parsed


def _first_cycle_outlook(
    value: Any, cycle_start: str | None, errors: _Errors | None = None
) -> list[dict[str, Any]]:
    """The three weeks after the first one, refused rather than defaulted if absent.

    The validator only warns about a short outlook, because a cycle already in flight can
    honestly have weeks it has not decided yet. A first plan cannot: it is being written
    right now, all at once, and a first answer that shows one week and calls it a 28-day
    direction is exactly the first-use failure #61 names. So this is an error here, with
    the count and the dates it wanted, because the fix is to send the missing weeks.
    """
    own = errors is None
    if errors is None:
        errors = _Errors()
    field = "initialization_request.cycle.outlook"
    before = len(errors.messages)
    weeks = _outlook(value, field, errors)
    # Date matching needs a parsed cycle start and a structurally valid outlook. A
    # malformed week is already named; do not pile a derived-date refusal on top of it.
    if cycle_start is not None and len(errors.messages) == before:
        start = dt.date.fromisoformat(cycle_start)
        expected = [(start + dt.timedelta(days=7 * n)).isoformat() for n in (1, 2, 3)]
        if [week["week_start"] for week in weeks] != expected:
            errors.messages.append(
                f"{field} must be the three weeks after the first one, in order: "
                + ", ".join(expected)
            )
    if own:
        errors.raise_collected()
    return weeks


def _evidence(value: Any, errors: _Errors | None = None) -> list[dict[str, str]]:
    own = errors is None
    if errors is None:
        errors = _Errors()
    field = "initialization_request.evidence"
    items = errors.try_(lambda: _array(value, field))
    evidence: list[dict[str, str]] = []
    if items is None:
        if own:
            errors.raise_collected()
        return []
    if not items:
        errors.messages.append(
            f"{field} must cite at least one thing the athlete told you"
        )
        if own:
            errors.raise_collected()
        return []
    for index, raw in enumerate(items):
        item_field = f"{field}[{index}]"
        item = errors.try_(lambda raw=raw, item_field=item_field: _object(raw, item_field))
        if item is None:
            continue
        before_keys = len(errors.messages)
        errors.try_(
            lambda item=item, item_field=item_field: _keys(
                item, item_field, ("field", "observation")
            )
        )
        if len(errors.messages) != before_keys:
            continue
        evidence.append(
            {
                "field": errors.try_(
                    lambda item=item, item_field=item_field: _text(
                        item.get("field"), f"{item_field}.field"
                    ),
                    default="",
                ),
                "observation": errors.try_(
                    lambda item=item, item_field=item_field: _text(
                        item.get("observation"), f"{item_field}.observation"
                    ),
                    default="",
                ),
            }
        )
    if own:
        errors.raise_collected()
    return evidence


# --------------------------------------------------------------------------------------
# The first week's sessions
# --------------------------------------------------------------------------------------


def _session(raw: Any, field: str, taken: set[str], language: str) -> dict[str, Any]:
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
    # Carried only when the coach wrote one, which is how a changed session carries it
    # too (`plan_change._coach_note`): a string sets it, and absent or null leaves the
    # session without one rather than with an empty one. The note travels to the
    # athlete's calendar at the end of the entry description, so a first plan that could
    # not carry it delivered a week of workouts with nothing said about them.
    note = session_request.get("coach_note")
    if note is not None:
        session["coach_note"] = _text(note, f"{field}.coach_note")
    # Rendered from the plan, never taken from the request: the athlete's first plan is
    # held to the same rule as every later one -- prose is an output.
    session["prescription"] = render_prescription(session["plan"], language)
    # A session publishes exactly when delivery could send it: the workout a run's
    # time_axis plan describes, or the purpose that titles a strength calendar entry.
    # The flag is a fact about the session, not something a request may assert.
    session["execution"]["publish_supported"] = _publish_supported(session)
    return session


def _sessions(
    value: Any, language: str, errors: _Errors | None = None
) -> list[dict[str, Any]]:
    own = errors is None
    if errors is None:
        errors = _Errors()
    field = "initialization_request.sessions"
    operations = errors.try_(lambda: _array(value, field))
    sessions: list[dict[str, Any]] = []
    if operations is None:
        if own:
            errors.raise_collected()
        return []
    if not operations:
        errors.messages.append(
            f"{field} must contain the first week's sessions; there is no default week"
        )
        if own:
            errors.raise_collected()
        return []
    for index, raw in enumerate(operations):
        taken = {str(item["session_id"]) for item in sessions}
        session = errors.try_(
            lambda raw=raw, index=index, taken=taken: _session(
                raw, f"{field}[{index}]", taken, language
            )
        )
        if session is not None:
            _insert_in_date_order(sessions, session)
    if own:
        errors.raise_collected()
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
        "decision_scope": "cycle",
        "plan_id": plan["plan_id"],
        "plan_version": plan["version"],
        "goal": plan["goal"],
        "cycle": {
            name: cycle[name]
            for name in ("start", "end", "primary_adaptation", "maintenance_adaptation")
        },
        "week": {"start": week["start"], "intent": week["intent"]},
        # The other three weeks, in the preview the athlete confirms rather than in a
        # follow-up question. Confirming a 28-day direction they have not seen is
        # confirming the word "28".
        "outlook": cycle["outlook"],
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
    language: str = DEFAULT_LANGUAGE,
) -> dict[str, Any]:
    """Turn one coaching initialization request into a candidate first PlanState.

    Pure and total: the same request, instant and language always produce the same plan,
    byte for byte, which is what lets one confirmation bind a plan the agent never holds.
    ``language`` is one of those three inputs rather than a formatting choice made later:
    the prescriptions it renders are stored in the plan, so preparing and applying must
    pass the same value or the confirmed preview and the committed plan differ.
    """
    request = _object(initialization_request, "initialization_request")
    errors = _Errors()
    errors.try_(
        lambda: _keys(request, "initialization_request", _REQUIRED_FIELDS, _OPTIONAL_FIELDS)
    )
    absent = set(_REQUIRED_FIELDS) - request.keys()

    def unless_absent(name: str, check, *, default=None):
        return default if name in absent else errors.try_(check, default=default)

    summary = unless_absent(
        "summary",
        lambda: _text(request.get("summary"), "initialization_request.summary"),
        default="",
    )
    evidence = unless_absent(
        "evidence", lambda: _evidence(request.get("evidence"), errors), default=[]
    )
    availability = errors.try_(lambda: _availability(request.get("availability")))
    baseline = errors.try_(
        lambda: _athlete_baseline(request.get("baselines"), errors),
        default={
            name: None for name in (*_BASELINE_INTEGERS, *_BASELINE_NUMBERS)
        } | {"strength_loads": []},
    )
    goal = unless_absent(
        "goal",
        lambda: _goal(request.get("goal"), errors),
        default={"outcome": "", "measurement_protocol": ""},
    )
    cycle = unless_absent("cycle", lambda: _cycle(request.get("cycle"), errors))
    week_intent = unless_absent(
        "week_intent",
        lambda: _text(request.get("week_intent"), "initialization_request.week_intent"),
        default="",
    )
    if request.get("week_start") is not None:
        stated_week_start = errors.try_(
            lambda: _date(request.get("week_start"), "initialization_request.week_start")
        )
        derived_week_start = None if cycle is None else cycle.get("start")
        if (
            stated_week_start is not None
            and derived_week_start
            and stated_week_start != derived_week_start
        ):
            errors.messages.append(
                "initialization_request.week_start must be "
                f"{derived_week_start} -- a first week starts when its cycle does, so "
                f"{stated_week_start} describes a different one. Send that start "
                "instead, or leave week.start out and it is derived."
            )
    sessions = unless_absent(
        "sessions", lambda: _sessions(request.get("sessions"), language, errors), default=[]
    )
    unknowns = errors.try_(
        lambda: _text_array(
            request.get("unknowns") or [], "initialization_request.unknowns"
        ),
        default=[],
    )
    # Independently decidable request-shape failures have all been named. Nothing below
    # may run against a body already known to be malformed -- cycle.end, week.start and
    # outlook dates are collected above; PlanState validation still fail-closes after.
    errors.raise_collected()
    if cycle is None or baseline is None or goal is None:
        raise ChangeRequestError("initialization_request could not be projected")

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
        "goal": goal,
        "cycle": cycle,
        "week": {
            # The first week of a 28-day block starts when the block does.
            "start": cycle["start"],
            "intent": week_intent,
            "sessions": sessions,
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
