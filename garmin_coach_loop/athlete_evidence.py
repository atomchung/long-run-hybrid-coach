"""Facts the athlete states in conversation that no device and no provider holds.

Two of them, and deliberately only two (issues #28 and #47):

- **Which weekdays the athlete can train.** Intervals knows what was trained, never what
  next Tuesday looks like. Today that fact reaches the coach only inside one request --
  ``constraints.available_days`` -- so it dies with the conversation that carried it, and
  the next conversation asks again. A recurring default plus a single-week override is
  the whole model: weekday granularity, no clock time, no calendar integration, no
  scheduling engine. "Wednesday is gone this week" and "I train Mon/Wed/Fri" are
  different statements with different lifetimes, which is why there are two of them and
  not one.
- **What the athlete says they lifted.** ``strength_execution`` already exists as an
  evidence group, but its only producer reads one machine's local health.db
  (``source_personal_os.fetch_strength_execution``). A hosted athlete has no such file,
  so on that entry the group is permanently null and the coach judges strength work from
  duration and average heart rate. An athlete who says "bench 65 by 4" is reporting the
  one thing no provider will ever supply, and this stores it in the shape the coach
  already reads.

Everything here is *reported*, never measured, and it says so: every record carries
``source: "athlete_reported"`` and the instant it was recorded. Nothing in this module
scores, aggregates, compares against a baseline, or decides anything -- it is storage for
a statement, and what the statement means is the coach's judgment (AGENTS.md 4).

**Where it lives.** One JSON file, ``athlete-evidence.json``, beside the owner's
``store.json`` in the same private state directory. Not inside the append-only commit
chain: a plan revision is a coaching decision with a before and an after to validate,
while "I can't train Wednesday" is neither, and wrapping it in one would put athlete
statements through the approval boundary that exists for plan writes. Being a separate
file is also why this is additive -- ``store._inspect_store`` reads ``store.json`` and
``commits/`` and nothing else, so a store carrying this file opens unchanged on code
that has never heard of it, and ``WRITER_CONTRACT_VERSION`` does not move.

**Missing versus unreadable.** No file means no evidence, which is a perfectly ordinary
state and never an error -- an athlete who has said nothing has said nothing. A file that
exists but cannot be parsed, or does not hold the shape written here, is a different
thing entirely and raises: reading it as "no evidence" would silently drop statements the
athlete believes the coach still has (AGENTS.md 3). That is the same stance
``store._read_object`` takes, and the same error type, so a caller already handling a
broken store handles a broken evidence file identically.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .context_core import DEFAULT_TIMEZONE, BuildWindow
from .store import (
    ATHLETE_EVIDENCE_FILE,
    StateStoreError,
    _atomic_json,
    _exclusive_lock,
    _read_object,
    _utc_stamp,
    canonical_hash,
    resolve_state_root,
)


__all__ = [
    "ATHLETE_EVIDENCE_FILE",
    "ATHLETE_EVIDENCE_VERSION",
    "ATHLETE_REPORTED_SOURCE",
    "WEEKDAYS",
    "AthleteEvidenceError",
    "athlete_today",
    "effective_availability",
    "evidence_path",
    "load_evidence",
    "normalize_weekday",
    "record_availability",
    "record_strength_report",
    "strength_execution_from_reports",
    "week_start_for",
]


# The filename lives in ``store`` (re-exported here for readers of this module) because
# ``init_store`` has to recognise it: an athlete may state their days before a plan
# exists, and a directory holding only this file is not a directory already in use.
ATHLETE_EVIDENCE_VERSION = 1

# The provenance every record here carries, and the ``source`` a strength_execution group
# built from these reports declares. It is never "garmin" or "personal-os": what makes
# this evidence usable is precisely that the coach can see it came from the athlete's own
# account of the session rather than from a device.
ATHLETE_REPORTED_SOURCE = "athlete_reported"

WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")

# Full English names are accepted because one caller cannot avoid them: the
# initialization request's ``availability.days`` is free prose the coach transcribed, and
# an athlete who said "Monday and Thursday" should not lose that fact to a spelling rule.
# Nothing wider is accepted -- an unrecognised day is reported, never guessed at.
_WEEKDAY_ALIASES = {
    "monday": "mon",
    "tuesday": "tue",
    "wednesday": "wed",
    "thursday": "thu",
    "friday": "fri",
    "saturday": "sat",
    "sunday": "sun",
}

_AVAILABILITY_DAY_FIELDS = ("available_days", "unavailable_days")
_STRENGTH_SET_FIELDS = ("set", "weight_kg", "assist_kg", "reps", "rpe")


class AthleteEvidenceError(RuntimeError):
    """One athlete-reported statement was refused before anything was written.

    Only ever raised for the content of a statement -- an unknown weekday, a week that
    has already passed, a set with no number. A file that cannot be read raises
    ``StateStoreError`` instead, because those are different problems with different
    answers: the first is fixed by asking the athlete again, the second is not.
    """


# --------------------------------------------------------------------------------------
# Small pure helpers
# --------------------------------------------------------------------------------------


def normalize_weekday(value: Any) -> str | None:
    """Return the canonical three-letter weekday, or ``None`` when it is not one."""
    if not isinstance(value, str):
        return None
    lowered = value.strip().lower()
    if lowered in WEEKDAYS:
        return lowered
    return _WEEKDAY_ALIASES.get(lowered)


def week_start_for(day: dt.date) -> dt.date:
    """The Monday of the natural week ``day`` sits in.

    The athlete's week runs Monday to Sunday, the same frame ``review_frame`` uses. A
    rolling seven-day span counted back from today would answer a different question and
    would make an override drift by a day every time it was read.
    """
    return day - dt.timedelta(days=day.weekday())


def athlete_today(timezone_name: str, now: dt.datetime | None = None) -> dt.date:
    """Today in the athlete's own timezone, never the server's.

    Which week is "this week" and which day is "not in the future" are both athlete-local
    questions. A server in another timezone answering them from its own clock would
    refuse a Sunday-evening report as tomorrow's, or accept a week that has already
    started as still ahead.
    """
    if not isinstance(timezone_name, str) or not timezone_name.strip():
        raise AthleteEvidenceError("timezone must be a non-empty string")
    try:
        zone = ZoneInfo(timezone_name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise AthleteEvidenceError(f"unknown timezone: {timezone_name!r}") from exc
    moment = now if now is not None else dt.datetime.now(dt.timezone.utc)
    return moment.astimezone(zone).date()


def _recorded_at(now: dt.datetime | None) -> str:
    """The instant a statement is recorded, at the resolution the store already uses.

    Derived from the caller's own instant when one is given, so the record agrees with the
    request that produced it and a test can place two statements in a known order.
    """
    if now is None:
        return _utc_stamp()
    return (
        now.astimezone(dt.timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def evidence_path(state_dir: Path | str) -> Path:
    """The one file this module owns, inside the owner's own private state directory."""
    return resolve_state_root(state_dir) / ATHLETE_EVIDENCE_FILE


def empty_evidence() -> dict[str, Any]:
    """The shape of "this athlete has never reported anything"."""
    return {
        "athlete_evidence_version": ATHLETE_EVIDENCE_VERSION,
        "availability": {"recurring": None, "week_overrides": []},
        "strength_reports": [],
    }


def _unreadable(detail: str) -> StateStoreError:
    return StateStoreError(f"cannot read {ATHLETE_EVIDENCE_FILE}: {detail}")


def _validated_evidence(value: dict[str, Any]) -> dict[str, Any]:
    """Check the structure this module wrote is the structure it is reading back.

    Containers only. Each record's contents were validated on the way in, and the readers
    below re-check the few fields they cannot work without -- so a hand-edited file
    degrades one unreadable record rather than the whole account. Getting the containers
    wrong is different: nothing can be read at all, and pretending otherwise would report
    an athlete with statements on record as one who has made none.
    """
    version = value.get("athlete_evidence_version")
    if version != ATHLETE_EVIDENCE_VERSION:
        raise _unreadable(
            f"athlete_evidence_version must be {ATHLETE_EVIDENCE_VERSION}, found {version!r}"
        )
    availability = value.get("availability")
    if not isinstance(availability, dict):
        raise _unreadable("availability must be an object")
    recurring = availability.get("recurring")
    if recurring is not None and not isinstance(recurring, dict):
        raise _unreadable("availability.recurring must be an object or null")
    overrides = availability.get("week_overrides")
    if not isinstance(overrides, list) or not all(isinstance(item, dict) for item in overrides):
        raise _unreadable("availability.week_overrides must be an array of objects")
    reports = value.get("strength_reports")
    if not isinstance(reports, list) or not all(isinstance(item, dict) for item in reports):
        raise _unreadable("strength_reports must be an array of objects")
    return {
        "athlete_evidence_version": ATHLETE_EVIDENCE_VERSION,
        "availability": {"recurring": recurring, "week_overrides": list(overrides)},
        "strength_reports": list(reports),
    }


def load_evidence(state_dir: Path | str) -> dict[str, Any]:
    """Read this owner's reported evidence, or the empty shape when there is none.

    Never creates the file or the directory: a read must be able to answer "nothing
    reported" for an account that does not exist yet without bringing one into being.
    Raises ``StateStoreError`` when the file exists and cannot be read as what this module
    writes -- see the module docstring on why that is not degraded to "empty".
    """
    path = evidence_path(state_dir)
    if not path.is_file():
        return empty_evidence()
    return _validated_evidence(_read_object(path))


# --------------------------------------------------------------------------------------
# Availability
# --------------------------------------------------------------------------------------


def _day_lists(value: dict[str, Any], field: str) -> tuple[list[str], list[str]]:
    """Validate one availability statement's two day lists as a pair.

    They are checked together because the only contradiction that matters spans both:
    naming the same day as available and unavailable is not a stricter constraint, it is
    two statements that cannot both be true, and storing it would leave the coach to pick
    one. An empty pair is refused for the same reason a blank answer is not an answer --
    a record that names no day says nothing, and would still overwrite a recurring
    default that did.
    """
    unexpected = sorted(set(value) - set(_AVAILABILITY_DAY_FIELDS))
    if unexpected:
        raise AthleteEvidenceError(f"{field} does not accept {', '.join(unexpected)}")
    parsed: dict[str, list[str]] = {}
    for name in _AVAILABILITY_DAY_FIELDS:
        raw = value.get(name)
        if raw is None:
            parsed[name] = []
            continue
        if not isinstance(raw, list):
            raise AthleteEvidenceError(f"{field}.{name} must be an array of weekday names")
        days: list[str] = []
        for item in raw:
            day = normalize_weekday(item)
            if day is None:
                raise AthleteEvidenceError(f"{field}.{name} contains an unknown weekday: {item!r}")
            if day not in days:
                days.append(day)
        parsed[name] = sorted(days, key=WEEKDAYS.index)
    available = parsed["available_days"]
    unavailable = parsed["unavailable_days"]
    both = sorted(set(available) & set(unavailable), key=WEEKDAYS.index)
    if both:
        raise AthleteEvidenceError(
            f"{field} names {', '.join(both)} as both available and unavailable"
        )
    if not available and not unavailable:
        raise AthleteEvidenceError(f"{field} must name at least one weekday")
    return available, unavailable


def _availability_record(
    available: list[str], unavailable: list[str], *, recorded_at: str
) -> dict[str, Any]:
    return {
        "available_days": available,
        "unavailable_days": unavailable,
        "recorded_at": recorded_at,
        "source": ATHLETE_REPORTED_SOURCE,
    }


def _week_start(value: Any, *, today: dt.date) -> dt.date:
    if not isinstance(value, str):
        raise AthleteEvidenceError("week_override.week_start must be an ISO date")
    try:
        parsed = dt.date.fromisoformat(value)
    except ValueError as exc:
        raise AthleteEvidenceError(
            f"week_override.week_start must be an ISO date: {value!r}"
        ) from exc
    if parsed.weekday() != 0:
        raise AthleteEvidenceError("week_override.week_start must be a Monday")
    if parsed < week_start_for(today):
        # A week that has already ended cannot be planned, and accepting one would let a
        # mistyped date sit in the file looking like a statement about the future.
        # Overrides already stored for past weeks are kept, not deleted -- they are the
        # record of what the athlete said at the time, and they simply stop matching.
        raise AthleteEvidenceError("week_override.week_start is a week that has already passed")
    return parsed


def record_availability(
    state_dir: Path | str,
    *,
    recurring: dict[str, Any] | None = None,
    week_override: dict[str, Any] | None = None,
    timezone_name: str = DEFAULT_TIMEZONE,
    now: dt.datetime | None = None,
) -> dict[str, Any]:
    """Store which weekdays the athlete can train, and answer with what now holds.

    ``recurring`` is a single latest-wins value: an athlete who moves their training days
    has not created a second schedule, and keeping both would leave a reader to guess
    which is current. Provenance survives the overwrite through ``recorded_at`` and
    ``source`` on the record itself.

    ``week_override`` is append-only, because it is a statement about one specific week
    and the file is the only place it exists. Corrections are made by recording the week
    again -- the newest ``recorded_at`` inside a week wins when it is read back, and the
    earlier statement stays visible as what was said before.

    Writes nothing when either statement is refused: both are validated before the file is
    opened, so a bad override never lands a good recurring value half-applied. There is
    deliberately no requirement that a plan exist first -- an athlete may well say which
    days they train before deciding what to train on them.
    """
    if recurring is None and week_override is None:
        raise AthleteEvidenceError("record_availability needs recurring, week_override, or both")

    today = athlete_today(timezone_name, now)
    recurring_days: tuple[list[str], list[str]] | None = None
    if recurring is not None:
        if not isinstance(recurring, dict):
            raise AthleteEvidenceError("recurring must be an object")
        recurring_days = _day_lists(recurring, "recurring")

    override_start: dt.date | None = None
    override_days: tuple[list[str], list[str]] | None = None
    if week_override is not None:
        if not isinstance(week_override, dict):
            raise AthleteEvidenceError("week_override must be an object")
        override_start = _week_start(week_override.get("week_start"), today=today)
        override_days = _day_lists(
            {key: value for key, value in week_override.items() if key != "week_start"},
            "week_override",
        )

    recorded_at = _recorded_at(now)
    root = resolve_state_root(state_dir)
    # 0o700 when this module creates it, matching init_store; an already-existing
    # directory keeps whatever the store gave it.
    root.mkdir(parents=True, mode=0o700, exist_ok=True)
    recorded_override: dict[str, Any] | None = None
    with _exclusive_lock(root):
        evidence = load_evidence(root)
        if recurring_days is not None:
            evidence["availability"]["recurring"] = _availability_record(
                *recurring_days, recorded_at=recorded_at
            )
        if override_days is not None and override_start is not None:
            recorded_override = {
                "week_start": override_start.isoformat(),
                **_availability_record(*override_days, recorded_at=recorded_at),
            }
            evidence["availability"]["week_overrides"].append(recorded_override)
        _atomic_json(evidence_path(root), evidence)

    return {
        "athlete_evidence_version": ATHLETE_EVIDENCE_VERSION,
        "recurring": evidence["availability"]["recurring"],
        "week_override": recorded_override,
        "effective_this_week": effective_availability(
            evidence, week_start=week_start_for(today)
        ),
    }


def effective_availability(
    evidence: dict[str, Any], *, week_start: dt.date
) -> dict[str, Any] | None:
    """Which weekdays hold for the week beginning ``week_start``, or ``None`` if unstated.

    An override for that exact week wins over the recurring default -- that is the point
    of recording one. Within a week the newest ``recorded_at`` wins, so a correction is a
    second statement rather than an edit. An override for any other week is not consulted
    at all: it neither applies nor, having been superseded by the calendar rather than by
    a later statement, cancels the recurring default it once replaced.
    """
    availability = evidence.get("availability") or {}
    target = week_start.isoformat()
    matches = [
        (index, item)
        for index, item in enumerate(availability.get("week_overrides") or [])
        if isinstance(item, dict) and item.get("week_start") == target
    ]
    if matches:
        # Position breaks a tie, because ``recorded_at`` has second resolution and two
        # corrections inside one second must still resolve to the later one. The list is
        # append-only, so a later position is a later statement by construction.
        _, newest = max(matches, key=lambda pair: (str(pair[1].get("recorded_at") or ""), pair[0]))
        return {
            "week_start": target,
            "available_days": list(newest.get("available_days") or []),
            "unavailable_days": list(newest.get("unavailable_days") or []),
            "basis": "week_override",
            "recorded_at": newest.get("recorded_at"),
            "source": newest.get("source"),
        }
    recurring = availability.get("recurring")
    if isinstance(recurring, dict):
        return {
            "week_start": target,
            "available_days": list(recurring.get("available_days") or []),
            "unavailable_days": list(recurring.get("unavailable_days") or []),
            "basis": "recurring",
            "recorded_at": recurring.get("recorded_at"),
            "source": recurring.get("source"),
        }
    return None


# --------------------------------------------------------------------------------------
# Athlete-reported strength execution
# --------------------------------------------------------------------------------------


def _strength_set(value: Any, index: int) -> dict[str, Any]:
    """Validate one reported set against the shape ``strength_execution`` already holds.

    Unknown keys are refused rather than ignored: a mistyped ``weigth_kg`` that was
    quietly dropped would store a set the athlete believes carries a load and the coach
    reads as bodyweight. The four measurements may be omitted and become null -- a
    bodyweight set genuinely has no weight and a set counted without an RPE genuinely has
    no RPE, and demanding four explicit nulls per set buys no safety. ``set`` alone is
    required, because a set with no position in the session cannot be ordered against the
    others.
    """
    field = f"sets[{index}]"
    if not isinstance(value, dict):
        raise AthleteEvidenceError(f"{field} must be an object")
    unexpected = sorted(set(value) - set(_STRENGTH_SET_FIELDS))
    if unexpected:
        raise AthleteEvidenceError(f"{field} does not accept {', '.join(unexpected)}")
    number = value.get("set")
    if isinstance(number, bool) or not isinstance(number, int) or number < 1:
        raise AthleteEvidenceError(f"{field}.set must be an integer >= 1")
    result: dict[str, Any] = {"set": number}
    for name in ("weight_kg", "assist_kg", "rpe"):
        raw = value.get(name)
        if raw is not None and (isinstance(raw, bool) or not isinstance(raw, (int, float))):
            raise AthleteEvidenceError(f"{field}.{name} must be a number or null")
        result[name] = raw
    reps = value.get("reps")
    if reps is not None and (isinstance(reps, bool) or not isinstance(reps, int)):
        raise AthleteEvidenceError(f"{field}.reps must be an integer or null")
    result["reps"] = reps
    return {name: result[name] for name in _STRENGTH_SET_FIELDS}


def record_strength_report(
    state_dir: Path | str,
    *,
    date: Any,
    exercise: Any,
    category: Any,
    sets: Any,
    notes: Any = None,
    timezone_name: str = DEFAULT_TIMEZONE,
    now: dt.datetime | None = None,
) -> dict[str, Any]:
    """Store one exercise the athlete says they performed, verbatim.

    ``report_id`` is derived from the content -- date, exercise, category, sets and notes
    -- rather than assigned, so re-sending the same report is a no-op that reports itself
    as one. A dropped response or a retried turn therefore cannot enter the same three
    sets twice, and no client has to hold an idempotency key across a conversation.

    The date may not be in the athlete's future. Everything else is taken as stated: no
    weight is checked against ``athlete_baseline.strength_loads``, no set is marked
    complete or short, nothing is summed. What five sets at 65 kg mean is the coach's
    judgment, and a product that scored it here would be judging in the store
    (AGENTS.md 4, 5).
    """
    if not isinstance(date, str):
        raise AthleteEvidenceError("date must be an ISO date")
    try:
        parsed_date = dt.date.fromisoformat(date)
    except ValueError as exc:
        raise AthleteEvidenceError(f"date must be an ISO date: {date!r}") from exc
    today = athlete_today(timezone_name, now)
    if parsed_date > today:
        raise AthleteEvidenceError("date is in the future for this athlete")

    for name, value in (("exercise", exercise), ("category", category)):
        if not isinstance(value, str) or not value.strip():
            raise AthleteEvidenceError(f"{name} must be a non-empty string")
    if not isinstance(sets, list) or not sets:
        raise AthleteEvidenceError("sets must be a non-empty array")
    parsed_sets = [_strength_set(item, index) for index, item in enumerate(sets)]

    raw_notes = notes if notes is not None else []
    if not isinstance(raw_notes, list) or not all(
        isinstance(item, str) and item.strip() for item in raw_notes
    ):
        raise AthleteEvidenceError("notes must be an array of non-empty strings")

    content = {
        "date": parsed_date.isoformat(),
        "exercise": exercise,
        "category": category,
        "sets": parsed_sets,
        "notes": list(raw_notes),
    }
    report_id = canonical_hash(content)

    root = resolve_state_root(state_dir)
    # 0o700 when this module creates it, matching init_store; an already-existing
    # directory keeps whatever the store gave it.
    root.mkdir(parents=True, mode=0o700, exist_ok=True)
    with _exclusive_lock(root):
        evidence = load_evidence(root)
        existing = next(
            (
                item
                for item in evidence["strength_reports"]
                if item.get("report_id") == report_id
            ),
            None,
        )
        if existing is not None:
            return {
                "report_id": report_id,
                "idempotent_replay": True,
                "report": existing,
                "report_count": len(evidence["strength_reports"]),
            }
        report = {
            "report_id": report_id,
            **content,
            "recorded_at": _recorded_at(now),
            "source": ATHLETE_REPORTED_SOURCE,
        }
        evidence["strength_reports"].append(report)
        _atomic_json(evidence_path(root), evidence)
        return {
            "report_id": report_id,
            "idempotent_replay": False,
            "report": report,
            "report_count": len(evidence["strength_reports"]),
        }


def strength_execution_from_reports(
    evidence: dict[str, Any], window: BuildWindow
) -> dict[str, Any] | None:
    """Assemble reported sets into the ``strength_execution`` group the coach already reads.

    ``None`` when nothing was reported inside the window, which the caller turns back into
    the ordinary "no strength evidence" unknown. This never competes with health.db: the
    caller only reaches here when no local strength log is configured at all, so a machine
    with one keeps reading per-set truth from it and an athlete without one stops being
    invisible.

    Same grouping and ordering rules as ``source_personal_os.fetch_strength_execution``,
    so a context built either way sorts identically: one entry per (date, exercise), dates
    newest first, exercises alphabetical within a date, sets ascending. Several reports for
    the same exercise on the same day -- the athlete adding sets as the session goes -- are
    concatenated in the order they were recorded and renumbered 1..N, because two reports
    each numbering their own sets from one would otherwise produce two "set 1"s in a
    session that had one.
    """
    reports = [
        item
        for item in (evidence.get("strength_reports") or [])
        if isinstance(item, dict)
    ]
    ordered = sorted(
        (
            (str(item.get("recorded_at") or ""), index, item)
            for index, item in enumerate(reports)
        ),
        key=lambda entry: (entry[0], entry[1]),
    )

    sessions_by_key: dict[tuple[dt.date, str], dict[str, Any]] = {}
    for _, _, report in ordered:
        raw_date = report.get("date")
        try:
            day = dt.date.fromisoformat(str(raw_date))
        except ValueError:
            continue
        if not (window.window42_start <= day <= window.window42_end):
            continue
        # The three fields a session cannot be built without. A record missing one was
        # never written by ``record_strength_report``, which requires all three; skipping
        # it keeps the group valid rather than producing one that fails the CoachContext
        # contract and blocks the whole build over a single hand-edited row.
        exercise = report.get("exercise")
        category = report.get("category")
        if not isinstance(exercise, str) or not exercise.strip():
            continue
        if not isinstance(category, str) or not category.strip():
            continue
        key = (day, exercise)
        session = sessions_by_key.get(key)
        if session is None:
            session = {
                "date": day,
                "exercise": exercise,
                # The first report of the day names the category; a later one restating it
                # differently is not a reason to relabel what is already written down.
                "category": category,
                "sets": [],
                "notes": [],
            }
            sessions_by_key[key] = session
        for item in report.get("sets") or []:
            if isinstance(item, dict):
                session["sets"].append(dict(item))
        for note in report.get("notes") or []:
            if isinstance(note, str) and note not in session["notes"]:
                session["notes"].append(note)

    if not sessions_by_key:
        return None

    ordered_keys = sorted(sessions_by_key, key=lambda item: (-item[0].toordinal(), item[1]))
    sessions: list[dict[str, Any]] = []
    for key in ordered_keys:
        session = sessions_by_key[key]
        sessions.append(
            {
                "date": session["date"].isoformat(),
                "exercise": session["exercise"],
                "category": session["category"],
                "sets": [
                    {**item, "set": number}
                    for number, item in enumerate(session["sets"], start=1)
                ],
                "notes": session["notes"],
            }
        )

    return {
        "source": ATHLETE_REPORTED_SOURCE,
        "window_start": window.window42_start.isoformat(),
        "window_end": window.window42_end.isoformat(),
        "sessions": sessions,
    }
