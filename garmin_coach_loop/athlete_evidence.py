"""Facts the athlete states in conversation that no device and no provider holds.

Two of them, and deliberately only two (issues #28 and #47):

- **Which weekdays the athlete can train.** Intervals knows what was trained, never what
  next Tuesday looks like. Today that fact reaches the coach only inside one request --
  ``constraints.available_days`` -- so it dies with the conversation that carried it, and
  the next conversation asks again. A recurring default plus per-week statements is the
  whole model: weekday granularity, no clock time, no calendar integration, no scheduling
  engine. "Wednesday is gone this week" and "I train Mon/Wed/Fri" are different
  statements with different lifetimes, which is why there are two of them and not one.

  A week statement **layers onto** the recurring default rather than replacing it. This
  is the difference between an athlete saying one sentence and an athlete filling in a
  form: with Mon/Wed/Fri standing, "something came up Wednesday" has to leave Monday and
  Friday exactly where they were, or the coach must ask about days the athlete never
  mentioned. The exhaustive form exists too, because "this week I can only do Tue/Thu"
  is a genuinely different statement -- but it is the athlete's word "only" that selects
  it, never a mode the caller has to reason about.
- **What the athlete says they lifted.** ``strength_execution`` already exists as an
  evidence group, but its only producer reads one machine's local health.db
  (``source_personal_os.fetch_strength_execution``). A hosted athlete has no such file,
  so on that entry the group is permanently null and the coach judges strength work from
  duration and average heart rate. An athlete who says "bench 65 by 4" is reporting the
  one thing no provider will ever supply, and this stores it in the shape the coach
  already reads.

  What the provider *does* supply for a strength session is the session itself -- its
  date, how long it ran, the heart rate, and the athlete's own label for it ("chest day")
  -- and that arrives through ``recent_actuals`` on its own. So a report here is a
  *supplement* to evidence the coach already has, never a re-entry of the session: it
  needs the movement and the sets, and nothing else. Everything else it might ask for is
  either already known or not worth a turn of conversation.

  One record per movement per day, newest winning. "65, sorry, 70" is one set described
  twice, and a store that appended it would hand the coach twice the volume that was
  actually lifted.

  There are two ways that record arrives, and they are different claims (issue #76).
  Describing the sets is one. The other is confirming a session the plan already holds
  set for set -- "今天重訓照做了" -- where dictating them back is asking the athlete to
  read the prescription aloud, and the friction of that is what let strength evidence
  lapse for two and a half weeks and produced a phantom baseline. So a confirmation
  transcribes ``plan.movements`` and takes only the deviations, and the records say
  ``prescribed_confirmed`` rather than ``athlete_reported``: a coach reading a
  progression needs to know that a confirmed prescription tells them nothing the plan
  did not already say. Neither is a measurement, and neither displaces one.

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
from .validation import normalize_exercise_name
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
    "PRESCRIBED_CONFIRMED_SOURCE",
    "WEEKDAYS",
    "AthleteEvidenceError",
    "athlete_today",
    "confirm_prescribed_strength",
    "effective_availability",
    "evidence_path",
    "exercise_key",
    "load_evidence",
    "normalize_weekday",
    "record_availability",
    "record_strength_report",
    "reported_strength_sessions",
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

# The provenance of a session the athlete confirmed against its own prescription rather
# than described set by set (issue #76). Kept apart from ``athlete_reported`` because the
# two are different claims: one is "this is what I lifted", the other is "I did what the
# plan said". Both are the athlete's word and neither is a measurement, but a coach
# reading a progression needs to know which of the two it is looking at -- a confirmed
# prescription tells you nothing the plan did not already say.
PRESCRIBED_CONFIRMED_SOURCE = "prescribed_confirmed"

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

# What a week statement may carry beyond the two day lists above. ``only_days`` is the
# exhaustive form ("this week I can only do Tue/Thu"); it cannot be combined with the
# day lists, which are changes measured against the recurring default.
_WEEK_FIELDS = (*_AVAILABILITY_DAY_FIELDS, "only_days", "week_start")

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


def _weekday_list(raw: Any, field: str) -> list[str]:
    """One list of weekday names, canonicalised and put in week order."""
    if not isinstance(raw, list):
        raise AthleteEvidenceError(f"{field} must be an array of weekday names")
    days: list[str] = []
    for item in raw:
        day = normalize_weekday(item)
        if day is None:
            raise AthleteEvidenceError(f"{field} contains an unknown weekday: {item!r}")
        if day not in days:
            days.append(day)
    return sorted(days, key=WEEKDAYS.index)


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
    parsed = {
        name: _weekday_list(value.get(name) or [], f"{field}.{name}")
        for name in _AVAILABILITY_DAY_FIELDS
    }
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
    """Which week a week statement is about -- the current one unless told otherwise.

    Omitting it is the ordinary case and not a missing field: "something came up
    Wednesday" is about the week the athlete is standing in, and making the caller derive
    that Monday from a timezone is asking it to compute something this module already
    knows. An explicit date still works for "next Wednesday".

    A day inside the week is accepted, not only its Monday. The athlete says "Wednesday",
    and a caller that passes that Wednesday through means the week containing it; making
    that an error would buy nothing except a second call.
    """
    if value is None:
        return week_start_for(today)
    if not isinstance(value, str):
        raise AthleteEvidenceError("week.week_start must be an ISO date")
    try:
        parsed = dt.date.fromisoformat(value)
    except ValueError as exc:
        raise AthleteEvidenceError(f"week.week_start must be an ISO date: {value!r}") from exc
    start = week_start_for(parsed)
    if start < week_start_for(today):
        # A week that has already ended cannot be planned, and accepting one would let a
        # mistyped date sit in the file looking like a statement about the future. Week
        # statements already stored for past weeks are kept, not deleted -- they are the
        # record of what the athlete said at the time, and they simply stop matching.
        raise AthleteEvidenceError("week.week_start is a week that has already passed")
    return start


def _week_statement(value: dict[str, Any], *, today: dt.date) -> dict[str, Any]:
    """Validate one statement about a single week, in either of its two forms.

    The forms are mutually exclusive because they answer different questions. ``only_days``
    says what the whole week is; ``available_days``/``unavailable_days`` say what changed
    about it. Accepting both at once would leave the week's meaning to evaluation order.
    """
    unexpected = sorted(set(value) - set(_WEEK_FIELDS))
    if unexpected:
        raise AthleteEvidenceError(f"week does not accept {', '.join(unexpected)}")
    week_start = _week_start(value.get("week_start"), today=today)
    only_raw = value.get("only_days")
    changes = {key: value[key] for key in _AVAILABILITY_DAY_FIELDS if value.get(key)}
    if only_raw is not None:
        if changes:
            raise AthleteEvidenceError(
                "week accepts either only_days or available_days/unavailable_days, not both"
            )
        only_days = _weekday_list(only_raw, "week.only_days")
        if not only_days:
            # "I can train no days this week" is a real thing to say, but not through the
            # field whose name means "these are the days" -- it is unavailable_days.
            raise AthleteEvidenceError("week.only_days must name at least one weekday")
        return {
            "week_start": week_start.isoformat(),
            "only_days": only_days,
            "available_days": [],
            "unavailable_days": [],
        }
    available, unavailable = _day_lists(
        {key: value.get(key) for key in _AVAILABILITY_DAY_FIELDS}, "week"
    )
    return {
        "week_start": week_start.isoformat(),
        "only_days": None,
        "available_days": available,
        "unavailable_days": unavailable,
    }


def record_availability(
    state_dir: Path | str,
    *,
    recurring: dict[str, Any] | None = None,
    week: dict[str, Any] | None = None,
    timezone_name: str = DEFAULT_TIMEZONE,
    now: dt.datetime | None = None,
) -> dict[str, Any]:
    """Store which weekdays the athlete can train, and answer with what now holds.

    ``recurring`` is a single latest-wins value: an athlete who moves their training days
    has not created a second schedule, and keeping both would leave a reader to guess
    which is current. Provenance survives the overwrite through ``recorded_at`` and
    ``source`` on the record itself.

    ``week`` is append-only, because it is a statement about one specific week and the
    file is the only place it exists. Several statements about the same week compose in
    the order they were made, which is what makes "Wednesday's out" followed by "Friday
    too" behave the way the athlete means it -- see ``effective_availability``.

    Writes nothing when either statement is refused: both are validated before the file is
    opened, so a bad week never lands a good recurring value half-applied. There is
    deliberately no requirement that a plan exist first -- an athlete may well say which
    days they train before deciding what to train on them.
    """
    if recurring is None and week is None:
        raise AthleteEvidenceError("record_availability needs recurring, week, or both")

    today = athlete_today(timezone_name, now)
    recurring_days: tuple[list[str], list[str]] | None = None
    if recurring is not None:
        if not isinstance(recurring, dict):
            raise AthleteEvidenceError("recurring must be an object")
        recurring_days = _day_lists(recurring, "recurring")

    statement: dict[str, Any] | None = None
    if week is not None:
        if not isinstance(week, dict):
            raise AthleteEvidenceError("week must be an object")
        statement = _week_statement(week, today=today)

    recorded_at = _recorded_at(now)
    root = resolve_state_root(state_dir)
    # 0o700 when this module creates it, matching init_store; an already-existing
    # directory keeps whatever the store gave it.
    root.mkdir(parents=True, mode=0o700, exist_ok=True)
    recorded_week: dict[str, Any] | None = None
    with _exclusive_lock(root):
        evidence = load_evidence(root)
        if recurring_days is not None:
            evidence["availability"]["recurring"] = _availability_record(
                *recurring_days, recorded_at=recorded_at
            )
        if statement is not None:
            recorded_week = {
                **statement,
                "recorded_at": recorded_at,
                "source": ATHLETE_REPORTED_SOURCE,
            }
            evidence["availability"]["week_overrides"].append(recorded_week)
        _atomic_json(evidence_path(root), evidence)

    return {
        "athlete_evidence_version": ATHLETE_EVIDENCE_VERSION,
        "recurring": evidence["availability"]["recurring"],
        "week": recorded_week,
        "effective_this_week": effective_availability(
            evidence, week_start=week_start_for(today)
        ),
    }


def effective_availability(
    evidence: dict[str, Any], *, week_start: dt.date
) -> dict[str, Any] | None:
    """Which weekdays hold for the week beginning ``week_start``, or ``None`` if unstated.

    The recurring default is the starting point, and each statement about *this* week is
    applied on top of it in the order it was made. That layering is the whole design:
    with Mon/Wed/Fri standing, "Wednesday's out this week" has to answer Mon/Fri, not an
    empty week, or every such sentence costs the athlete a second round of questions
    about days they never brought up.

    Two shapes of statement, both athlete-shaped rather than mechanical:

    - ``only_days`` -- the week restated in full ("this week I can only do Tue/Thu"). It
      replaces whatever stood before it, and the recurring days it leaves out become
      this week's ``unavailable_days``, because that is what "only" said about them.
    - ``available_days`` / ``unavailable_days`` -- what changed ("Saturday's free too",
      "Wednesday's gone"). Each composes onto the running answer, so two sentences in two
      turns land the same as one sentence naming both.

    Statements about any other week are not consulted at all: one neither applies nor,
    having been superseded by the calendar rather than by a later statement, cancels the
    recurring default it once adjusted.
    """
    availability = evidence.get("availability") or {}
    target = week_start.isoformat()
    recurring = availability.get("recurring")
    has_recurring = isinstance(recurring, dict)

    available: list[str] = list(recurring.get("available_days") or []) if has_recurring else []
    unavailable: list[str] = list(recurring.get("unavailable_days") or []) if has_recurring else []
    recorded_at = recurring.get("recorded_at") if has_recurring else None
    source = recurring.get("source") if has_recurring else None
    basis = "recurring" if has_recurring else None

    # ``recorded_at`` has second resolution, so position breaks a tie: two statements
    # inside one second must still compose in the order they were made, and the list is
    # append-only so a later position is a later statement by construction.
    statements = sorted(
        (
            (str(item.get("recorded_at") or ""), index, item)
            for index, item in enumerate(availability.get("week_overrides") or [])
            if isinstance(item, dict) and item.get("week_start") == target
        ),
        key=lambda entry: (entry[0], entry[1]),
    )

    for _, _, item in statements:
        only_days = item.get("only_days")
        if isinstance(only_days, list) and only_days:
            # Everything the athlete normally trains, and anything a previous statement
            # this week had added, is out unless "only" named it.
            dropped = [day for day in (*available, *unavailable) if day not in only_days]
            available = [day for day in WEEKDAYS if day in set(only_days)]
            unavailable = [day for day in WEEKDAYS if day in set(dropped)]
        else:
            added = set(item.get("available_days") or [])
            removed = set(item.get("unavailable_days") or [])
            available = [
                day for day in WEEKDAYS if (day in set(available) or day in added) and day not in removed
            ]
            unavailable = [
                day
                for day in WEEKDAYS
                if (day in set(unavailable) or day in removed) and day not in added
            ]
        recorded_at = item.get("recorded_at")
        source = item.get("source")
        basis = "recurring_adjusted" if has_recurring else "week"

    if basis is None:
        return None
    return {
        "week_start": target,
        "available_days": available,
        "unavailable_days": unavailable,
        "basis": basis,
        "recorded_at": recorded_at,
        "source": source,
    }


# --------------------------------------------------------------------------------------
# Athlete-reported strength execution
# --------------------------------------------------------------------------------------


def _strength_set(value: Any, index: int) -> dict[str, Any]:
    """Validate one reported set against the shape ``strength_execution`` already holds.

    Unknown keys are refused rather than ignored: a mistyped ``weigth_kg`` that was
    quietly dropped would store a set the athlete believes carries a load and the coach
    reads as bodyweight. Every measurement may be omitted and become null -- a bodyweight
    set genuinely has no weight and a set counted without an RPE genuinely has no RPE,
    and demanding explicit nulls per set buys no safety.

    ``set`` may be omitted too, and then it is this set's position in the list. "Bench 65
    by 4" names one set without numbering it, and requiring a number would make the
    caller invent one -- which, before the report became an upsert, is exactly how two
    reports of the same exercise both arrived as "set 1".
    """
    field = f"sets[{index}]"
    if not isinstance(value, dict):
        raise AthleteEvidenceError(f"{field} must be an object")
    unexpected = sorted(set(value) - set(_STRENGTH_SET_FIELDS))
    if unexpected:
        raise AthleteEvidenceError(f"{field} does not accept {', '.join(unexpected)}")
    number = value.get("set")
    if number is None:
        number = index + 1
    elif isinstance(number, bool) or not isinstance(number, int) or number < 1:
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


def exercise_key(exercise: str) -> str:
    """The identity two records of the same movement share.

    Deliberately the same resolution ``movement_history`` and the baseline lookup already
    use, not a second one that agrees with it today. Three questions turn on whether two
    names are one movement -- does this report correct that one, does a recalled set
    displace a measured one, do these occurrences belong on one row -- and a product that
    answered them with two normalizers would eventually answer them differently for the
    same athlete. ``normalize_exercise_name`` folds case, separators and punctuation and
    keeps non-ASCII, which is what lets a Chinese movement name match at all.

    Nothing wider -- no synonym table, no stemming, no mapping of "bench" onto "bench
    press". Those would silently merge two movements the athlete keeps apart, which is a
    worse failure than storing two entries they can see.
    """
    return normalize_exercise_name(exercise)


def record_strength_report(
    state_dir: Path | str,
    *,
    exercise: Any,
    sets: Any,
    date: Any = None,
    category: Any = None,
    notes: Any = None,
    timezone_name: str = DEFAULT_TIMEZONE,
    now: dt.datetime | None = None,
) -> dict[str, Any]:
    """Store what the athlete says they lifted for one movement on one day.

    Only ``exercise`` and ``sets`` are required, because they are the only two things
    nothing else can supply. The day is today unless the athlete named another one. The
    ``category`` is optional metadata -- a plan or the provider's own session label
    usually implies it, and asking an athlete whether bench press counts as chest is
    asking them to fill in a form (see the module docstring).

    **One report per movement per day, and the newest wins.** The athlete correcting
    themselves -- "65, sorry, 70" -- is describing the same sets a second time, not doing
    a second set, and appending would leave the coach reading double the volume actually
    lifted. So a report for a ``(date, exercise)`` already on record replaces it, and
    says so through ``replaced``. Adding sets later in the day works the same way: send
    the movement's sets as they now stand, which is what the coach is holding anyway.

    ``report_id`` stays derived from the content, so re-sending an identical report is a
    no-op that reports itself as one and a retried turn cannot churn the record.

    The date may not be in the athlete's future. Everything else is taken as stated: no
    weight is checked against ``athlete_baseline.strength_loads``, no set is marked
    complete or short, nothing is summed. What five sets at 65 kg mean is the coach's
    judgment, and a product that scored it here would be judging in the store
    (AGENTS.md 4, 5).
    """
    today = athlete_today(timezone_name, now)
    if date is None:
        parsed_date = today
    else:
        if not isinstance(date, str):
            raise AthleteEvidenceError("date must be an ISO date")
        try:
            parsed_date = dt.date.fromisoformat(date)
        except ValueError as exc:
            raise AthleteEvidenceError(f"date must be an ISO date: {date!r}") from exc
        if parsed_date > today:
            raise AthleteEvidenceError("date is in the future for this athlete")

    if not isinstance(exercise, str) or not exercise.strip():
        raise AthleteEvidenceError("exercise must be a non-empty string")
    if category is not None and (not isinstance(category, str) or not category.strip()):
        raise AthleteEvidenceError("category must be a non-empty string or null")
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
    written = _upsert_strength_reports(
        state_dir, [content], source=ATHLETE_REPORTED_SOURCE, now=now
    )
    return {**written["movements"][0], "report_count": written["report_count"]}


def _upsert_strength_reports(
    state_dir: Path | str,
    contents: list[dict[str, Any]],
    *,
    source: str,
    now: dt.datetime | None,
) -> dict[str, Any]:
    """Write one or more movement records under the one-per-(date, movement) rule.

    Shared by both ways a movement's sets arrive -- described set by set, or confirmed
    from the prescription -- because the rule they have to obey is the same one, and a
    second copy of it would be a second place for "newest wins" to drift. One lock and
    one file write for the whole batch: confirming a four-movement session is one
    statement by the athlete, so it either lands or it does not.
    """
    root = resolve_state_root(state_dir)
    # 0o700 when this module creates it, matching init_store; an already-existing
    # directory keeps whatever the store gave it.
    root.mkdir(parents=True, mode=0o700, exist_ok=True)
    results: list[dict[str, Any]] = []
    with _exclusive_lock(root):
        evidence = load_evidence(root)
        reports = evidence["strength_reports"]
        changed = False
        for content in contents:
            report_id = canonical_hash(content)
            key = (content["date"], exercise_key(content["exercise"]))
            position = next(
                (
                    index
                    for index, item in enumerate(reports)
                    if isinstance(item.get("exercise"), str)
                    and (str(item.get("date")), exercise_key(item["exercise"])) == key
                ),
                None,
            )
            if position is not None and reports[position].get("report_id") == report_id:
                results.append(
                    {
                        "report_id": report_id,
                        "idempotent_replay": True,
                        "replaced": None,
                        "report": reports[position],
                    }
                )
                continue
            report = {
                "report_id": report_id,
                **content,
                "recorded_at": _recorded_at(now),
                "source": source,
            }
            replaced: dict[str, Any] | None = None
            if position is None:
                reports.append(report)
            else:
                # The record it replaces is returned, never kept. Two versions of one
                # movement's sets in the file would put the coach back where appending
                # left it -- reading a correction as extra work -- and the athlete can
                # see in the response what their correction displaced.
                replaced = reports[position]
                reports[position] = report
            changed = True
            results.append(
                {
                    "report_id": report_id,
                    "idempotent_replay": False,
                    "replaced": replaced,
                    "report": report,
                }
            )
        if changed:
            _atomic_json(evidence_path(root), evidence)
        return {"movements": results, "report_count": len(reports)}


_DEVIATION_FIELDS = ("exercise", "set", "reps", "weight_kg", "assist_kg", "rpe")
_DEVIATION_MEASUREMENTS = ("reps", "weight_kg", "assist_kg", "rpe")


def _movement_sets(movement: dict[str, Any]) -> list[dict[str, Any]]:
    """The prescription's own sets, read as sets rather than re-derived.

    A movement already says how many sets, how many reps, and at what load; expanding it
    is transcription, not inference. ``load_kg`` absent stays absent -- a bodyweight
    movement has no weight, and writing one in would invent the number this whole record
    is careful not to claim was measured.
    """
    count = movement.get("sets")
    if isinstance(count, bool) or not isinstance(count, int) or count < 1:
        raise AthleteEvidenceError(
            f"movement {movement.get('display_name') or movement.get('exercise')!r} "
            "has no set count to confirm"
        )
    return [
        {
            "set": number,
            "weight_kg": movement.get("load_kg"),
            "assist_kg": movement.get("assist_kg"),
            "reps": movement.get("reps"),
            "rpe": None,
        }
        for number in range(1, count + 1)
    ]


def _apply_deviation(
    sets_by_exercise: dict[str, list[dict[str, Any]]],
    display_names: dict[str, str],
    raw: Any,
    index: int,
) -> None:
    field = f"deviations[{index}]"
    if not isinstance(raw, dict):
        raise AthleteEvidenceError(f"{field} must be an object")
    unexpected = sorted(set(raw) - set(_DEVIATION_FIELDS))
    if unexpected:
        raise AthleteEvidenceError(f"{field} does not accept {', '.join(unexpected)}")
    exercise = raw.get("exercise")
    if not isinstance(exercise, str) or not exercise.strip():
        raise AthleteEvidenceError(f"{field}.exercise must name a movement in this session")
    key = exercise_key(exercise)
    if key not in sets_by_exercise:
        raise AthleteEvidenceError(
            f"{field}.exercise {exercise!r} is not in this session, which prescribes "
            f"{', '.join(sorted(display_names.values()))}"
        )
    number = raw.get("set")
    if isinstance(number, bool) or not isinstance(number, int) or number < 1:
        raise AthleteEvidenceError(f"{field}.set must be an integer >= 1")
    prescribed = sets_by_exercise[key]
    if number > len(prescribed):
        raise AthleteEvidenceError(
            f"{field}.set {number} is beyond the {len(prescribed)} set(s) "
            f"{display_names[key]} prescribes"
        )
    overrides = {name: raw[name] for name in _DEVIATION_MEASUREMENTS if name in raw}
    if not overrides:
        # A deviation naming no measurement says the set differed without saying how,
        # which would be recorded as "exactly as prescribed" -- the opposite of what the
        # athlete just said.
        raise AthleteEvidenceError(
            f"{field} names no measurement that differed; give at least one of "
            f"{', '.join(_DEVIATION_MEASUREMENTS)}"
        )
    target = prescribed[number - 1]
    for name, value in overrides.items():
        if value is not None:
            if name == "reps":
                if isinstance(value, bool) or not isinstance(value, int):
                    raise AthleteEvidenceError(f"{field}.reps must be an integer or null")
            elif isinstance(value, bool) or not isinstance(value, (int, float)):
                raise AthleteEvidenceError(f"{field}.{name} must be a number or null")
        target[name] = value


def confirm_prescribed_strength(
    state_dir: Path | str,
    *,
    session: Any,
    deviations: Any = None,
    timezone_name: str = DEFAULT_TIMEZONE,
    now: dt.datetime | None = None,
) -> dict[str, Any]:
    """Record a prescribed strength session as done, with whatever differed.

    The gap this closes (issue #76): running is a closed loop -- the workout is delivered,
    the watch executes it, the activity returns, identity reconciles it. Lifting has no
    such return path. Garmin records no weight and no trustworthy set count, so the only
    evidence that a strength session happened as planned is the athlete saying so. Until
    now saying so meant dictating every set back, even though the plan already holds them
    set for set: the athlete was being asked to read the prescription aloud. Two and a half
    weeks of that friction is what produced a phantom 62.5 kg baseline.

    So this transcribes the session's own ``plan.movements`` into the evidence shape the
    coach already reads, and takes ``deviations`` for the parts that differed -- "照做，但
    臥推最後一組只推了 3 下" is one call. Everything not named as a deviation is recorded
    exactly as prescribed.

    **What this is, and what it is not.** The records carry
    ``source: "prescribed_confirmed"``, distinct from ``athlete_reported`` (recalled set by
    set) and from a local log's measured rows. It is the athlete's confirmation of a
    prescription, never a measurement, and the coach can see which it is holding. A local
    health.db entry for the same ``(date, exercise)`` still wins outright, unchanged --
    ``context_builder`` owns that precedence and this does not touch it.

    Deliberately narrow. It confirms the shape the plan prescribes; a session where a
    movement was skipped or swapped is not that session, and belongs in
    ``record_strength_report`` movement by movement. Refusing here is better than
    recording a prescription the athlete did not follow as though they had.

    Idempotent through the same content hash every other report uses, so confirming twice
    is a replay rather than a second record.
    """
    if not isinstance(session, dict):
        raise AthleteEvidenceError("session must be the plan's strength session object")
    if session.get("sport") != "strength":
        raise AthleteEvidenceError("only a strength session can be confirmed as prescribed")
    plan = session.get("plan")
    if not isinstance(plan, dict) or plan.get("kind") != "movement_list":
        raise AthleteEvidenceError(
            "this session prescribes no movements to confirm; report the movements that "
            "were trained instead"
        )
    movements = plan.get("movements")
    if not isinstance(movements, list) or not movements:
        raise AthleteEvidenceError("this session prescribes no movements to confirm")

    # The record belongs to the day the session was prescribed for, not to the day the
    # athlete happened to mention it: the sets are that session's. A session moved to
    # another day is a plan change, and it moves this with it.
    raw_date = session.get("scheduled_date")
    try:
        scheduled = dt.date.fromisoformat(str(raw_date))
    except ValueError as exc:
        raise AthleteEvidenceError(f"session scheduled_date is not an ISO date: {raw_date!r}") from exc
    if scheduled > athlete_today(timezone_name, now):
        raise AthleteEvidenceError(
            f"session {session.get('session_id')} is scheduled for {scheduled.isoformat()}, "
            "which is still in the future for this athlete"
        )

    sets_by_exercise: dict[str, list[dict[str, Any]]] = {}
    display_names: dict[str, str] = {}
    order: list[tuple[str, str]] = []
    for movement in movements:
        if not isinstance(movement, dict):
            raise AthleteEvidenceError("every prescribed movement must be an object")
        exercise = movement.get("exercise")
        if not isinstance(exercise, str) or not exercise.strip():
            raise AthleteEvidenceError("every prescribed movement must name an exercise")
        key = exercise_key(exercise)
        if key in sets_by_exercise:
            # A movement listed twice is ordinary programming, not a malformed plan: top
            # sets and then a back-off set is two rows for one movement, and the owner's
            # own plan does exactly that (`臥推 4x5 65公斤` then `臥推 1x5 60公斤`). Evidence
            # holds one record per movement per day, so the rows join into one continuous
            # run of sets -- which is also how the athlete counts them, making "the last
            # set" mean the last set of the movement rather than of whichever row it fell
            # in.
            existing = sets_by_exercise[key]
            for offset, item in enumerate(_movement_sets(movement), start=len(existing) + 1):
                existing.append({**item, "set": offset})
            continue
        sets_by_exercise[key] = _movement_sets(movement)
        display_names[key] = str(movement.get("display_name") or exercise)
        order.append((key, exercise))

    raw_deviations = deviations if deviations is not None else []
    if not isinstance(raw_deviations, list):
        raise AthleteEvidenceError("deviations must be an array")
    for index, deviation in enumerate(raw_deviations):
        _apply_deviation(sets_by_exercise, display_names, deviation, index)

    purpose = session.get("purpose")
    category = purpose.strip() if isinstance(purpose, str) and purpose.strip() else None
    contents = [
        {
            "date": scheduled.isoformat(),
            "exercise": exercise,
            "category": category,
            "sets": [
                {name: item[name] for name in _STRENGTH_SET_FIELDS}
                for item in sets_by_exercise[key]
            ],
            "notes": [],
        }
        for key, exercise in order
    ]
    written = _upsert_strength_reports(
        state_dir, contents, source=PRESCRIBED_CONFIRMED_SOURCE, now=now
    )
    return {
        "date": scheduled.isoformat(),
        "session_id": session.get("session_id"),
        "source": PRESCRIBED_CONFIRMED_SOURCE,
        "movements": written["movements"],
        "report_count": written["report_count"],
        "idempotent_replay": all(
            item["idempotent_replay"] for item in written["movements"]
        ),
    }


def reported_strength_sessions(
    evidence: dict[str, Any], window: BuildWindow
) -> list[dict[str, Any]]:
    """The reported movements inside ``window``, in ``strength_execution`` session shape.

    A list rather than a whole group, because these are merged alongside whatever a local
    strength log holds rather than used instead of it -- ``context_builder`` owns that
    join, and a hosted athlete simply has nothing on the other side of it.

    Same ordering as ``source_personal_os.fetch_strength_execution`` so a context built
    either way sorts identically: dates newest first, exercises alphabetical within a
    date, sets ascending. There is exactly one report per (date, exercise) --
    ``record_strength_report`` replaces rather than appends -- so nothing is concatenated
    or renumbered here, and a correction reads as the correction it was.

    Every session carries ``source: "athlete_reported"``. A record too damaged to place
    on a date, or naming no movement, is skipped rather than allowed to fail the whole
    build; a missing ``category`` is not damage, it is the ordinary case.
    """
    sessions: list[dict[str, Any]] = []
    for report in evidence.get("strength_reports") or []:
        if not isinstance(report, dict):
            continue
        try:
            day = dt.date.fromisoformat(str(report.get("date")))
        except ValueError:
            continue
        if not (window.window42_start <= day <= window.window42_end):
            continue
        exercise = report.get("exercise")
        if not isinstance(exercise, str) or not exercise.strip():
            continue
        category = report.get("category")
        sets = [
            {name: item.get(name) for name in _STRENGTH_SET_FIELDS}
            for item in report.get("sets") or []
            if isinstance(item, dict)
        ]
        sets.sort(key=lambda item: item["set"] if isinstance(item["set"], int) else 0)
        sessions.append(
            {
                "date": day.isoformat(),
                "exercise": exercise,
                "category": category if isinstance(category, str) and category.strip() else None,
                "sets": sets,
                "notes": [
                    note
                    for note in report.get("notes") or []
                    if isinstance(note, str) and note.strip()
                ],
                # Read off the record, not assumed: a confirmed prescription and a
                # set-by-set report live in the same file and the coach has to be able to
                # tell them apart. A record written before there was a second kind
                # carries none, and is what it was then.
                "source": (
                    report["source"]
                    if isinstance(report.get("source"), str) and report["source"].strip()
                    else ATHLETE_REPORTED_SOURCE
                ),
            }
        )
    sessions.sort(key=lambda item: (item["date"], item["exercise"]))
    sessions.sort(key=lambda item: item["date"], reverse=True)
    return sessions
