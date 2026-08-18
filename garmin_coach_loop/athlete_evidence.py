"""Facts the athlete states in conversation that no device and no provider holds.

- **Where the athlete is, and what language they read.** Both were deployment constants:
  one timezone compiled into the code, one language compiled into the renderer. Neither
  is a property of the deployment -- they are properties of the person -- and while they
  stayed constants a second athlete had to restate their timezone in every conversation,
  or silently live in somebody else's day, and received a plan they could not read on
  their watch. So the profile is stored exactly where the other athlete-stated facts are:
  said once, standing until restated, and visible to the coach in the context so that an
  athlete who has *not* said it can be asked rather than assumed about.

  Timezone is still accepted per request, but only as a one-off override -- "what does
  this look like where I am this week" -- and the stored value is what every entry falls
  back to. Language has no per-request form: a prescription is written once, stored, and
  later delivered, so a language that changed per request would leave one plan reading two
  ways.
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
  actually lifted. Correcting and retracting are different statements, not two names for
  one: "65, sorry, 70" replaces the record, "其實那天沒練" removes it. Retraction is
  refused the moment it also carries sets, because a statement that says nothing should
  stand there cannot also say what was lifted.
- **What the athlete weighs.** No provider this product reads holds a body composition
  figure, and the athlete has one the moment they step off a scale. It is stored raw --
  the number and the day -- with no trend, no rate of change, and no comparison against a
  target, because what a kilogram means for a hybrid block is the coach's reading of it
  and a store that computed a direction would have made that reading first.

  One record per day, and stating one measurement leaves the other exactly where it was:
  weight and body fat are two independent facts that happen to share a day, the same way
  timezone and language share a profile record. Bounds are refused rather than stored --
  a scale read as 7.23 kg is a typo, and a typo the coach is asked to interpret is worse
  than one the athlete is asked to repeat. The whole day's record can also be withdrawn
  rather than corrected, and the record it removes is echoed back in full so that a
  half-meant retraction -- only the weight was wrong -- can restate the half worth
  keeping.
- **A session no device recorded.** A pool without a watch, a hotel treadmill, a hike:
  training that happened and that Intervals will never hold. The athlete knows the real
  numbers -- how long, how far, how it felt -- and this stores them.

  It is deliberately *not* an actual. Nothing here enters ``recent_actuals``, completes a
  planned session, or counts toward the provider-derived coverage the freshness rows
  report; reconciliation never sees it. A report and a measured activity would otherwise
  be one session counted twice, and the loop's whole claim is that what it says came back
  is what the provider actually holds (AGENTS.md 8). So it sits beside provider evidence,
  labelled, and the coach weighs it as the athlete's word -- which is what it is.

  One summary per sport per day, newest winning, for the reason the strength report has
  the same rule: "40 分鐘，啊是 45" is one session described twice. Version 1 therefore
  cannot hold two genuinely distinct sessions of one sport on one day; the response names
  what a restatement displaced so a second session is never lost quietly, and two of them
  belong in one combined summary. "那筆游泳記錯了，拿掉" is a third kind of statement, not
  a third rule: it removes the day's summary for that sport outright rather than
  replacing it, and is refused if it also names a duration -- retracting and describing
  are two different sentences.

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
from .prescription import DEFAULT_LANGUAGE, LANGUAGES
from .validation import SPORTS, normalize_exercise_name
from .store import (
    ATHLETE_EVIDENCE_FILE,
    StateStoreError,
    _atomic_json,
    _exclusive_lock,
    _read_object,
    _refuse_when_handed_off,
    _utc_stamp,
    canonical_hash,
    resolve_state_root,
)


__all__ = [
    "ATHLETE_EVIDENCE_FILE",
    "ATHLETE_EVIDENCE_VERSION",
    "ATHLETE_REPORTED_SOURCE",
    "BODY_MEASUREMENT_BOUNDS",
    "PRESCRIBED_CONFIRMED_SOURCE",
    "PROFILE_FIELDS",
    "REPORTABLE_SPORTS",
    "WEEKDAYS",
    "AthleteEvidenceError",
    "athlete_today",
    "body_measurement_series",
    "confirm_prescribed_strength",
    "effective_availability",
    "evidence_path",
    "exercise_key",
    "load_evidence",
    "normalize_weekday",
    "profile_language",
    "profile_timezone",
    "record_activity_summary",
    "record_availability",
    "record_body_measurement",
    "record_profile",
    "record_strength_report",
    "reported_activity_summaries",
    "reported_strength_sessions",
    "resolve_settings",
    "retract_activity_summary",
    "retract_body_measurement",
    "retract_strength_report",
    "stored_profile",
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

# What a stored profile holds. ``timezone`` and ``language`` are independent statements
# that happen to live in one record: an athlete may state where they are without saying
# what they read, and stating one later must not erase the other.
PROFILE_FIELDS = ("timezone", "language", "recorded_at", "source")

_AVAILABILITY_DAY_FIELDS = ("available_days", "unavailable_days")

# What a week statement may carry beyond the two day lists above. ``only_days`` is the
# exhaustive form ("this week I can only do Tue/Thu"); it cannot be combined with the
# day lists, which are changes measured against the recurring default.
_WEEK_FIELDS = (*_AVAILABILITY_DAY_FIELDS, "only_days", "week_start")

_STRENGTH_SET_FIELDS = ("set", "weight_kg", "assist_kg", "reps", "rpe")

# What one day's body composition record may state. Both are optional individually and at
# least one is required, for the same reason the profile's two fields are: an athlete who
# weighed themselves has not thereby measured their body fat.
BODY_MEASUREMENT_VALUES = ("weight_kg", "body_fat_pct")

# The range each figure has to fall in to be a measurement rather than a typo. Wide on
# purpose -- this is not a plausibility model of an athlete, it is the boundary past which
# a number cannot be a reading of a scale at all, and anything inside it is stored exactly
# as stated. Refusing is the right answer over storing: a mistyped 7.23 kg reaches the
# coach as evidence of catastrophic weight loss, while a refusal reaches the athlete as
# one sentence asking them to say it again.
BODY_MEASUREMENT_BOUNDS: dict[str, tuple[float, float]] = {
    "weight_kg": (20.0, 400.0),
    "body_fat_pct": (1.0, 75.0),
}

# The sports an athlete can report having trained. Exactly the plan's own vocabulary
# (``validation.SPORTS``) minus rest, which is the same subtraction ``recent_actuals``
# makes and for the same reason: rest is not work, so there is no session to summarise. A
# parallel enum here would be a second answer to "what sports does this product know",
# and the two would eventually disagree about one.
REPORTABLE_SPORTS: tuple[str, ...] = tuple(sorted(SPORTS - {"rest"}))

_ACTIVITY_SUMMARY_FIELDS = (
    "date",
    "sport",
    "duration_minutes",
    "distance_km",
    "subjective_feel",
    "note",
)


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


def _zone(timezone_name: str) -> ZoneInfo:
    """One IANA zone, or a refusal naming the value that is not one.

    The check is the system's own zone database rather than a list kept here: a name this
    machine cannot resolve cannot answer "what day is it for this athlete" either, and a
    stored zone that only some hosts know would move the athlete's day between them.
    """
    try:
        return ZoneInfo(timezone_name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise AthleteEvidenceError(f"unknown timezone: {timezone_name!r}") from exc


def _reported_date(date: Any, *, today: dt.date) -> dt.date:
    """The day one report is about: today unless the athlete named another, never later.

    One rule for every statement about a day that has passed -- a lift, a weight, a
    session no device recorded -- because they are the same question and two copies of it
    would eventually answer differently about the same Sunday evening. The future is
    refused rather than stored: a plan is what says a day is coming, and evidence claiming
    to have observed one is a typed date the coach cannot tell from a real report.
    """
    if date is None:
        return today
    if not isinstance(date, str):
        raise AthleteEvidenceError("date must be an ISO date")
    try:
        parsed = dt.date.fromisoformat(date)
    except ValueError as exc:
        raise AthleteEvidenceError(f"date must be an ISO date: {date!r}") from exc
    if parsed > today:
        raise AthleteEvidenceError("date is in the future for this athlete")
    return parsed


def athlete_today(timezone_name: str, now: dt.datetime | None = None) -> dt.date:
    """Today in the athlete's own timezone, never the server's.

    Which week is "this week" and which day is "not in the future" are both athlete-local
    questions. A server in another timezone answering them from its own clock would
    refuse a Sunday-evening report as tomorrow's, or accept a week that has already
    started as still ahead.
    """
    if not isinstance(timezone_name, str) or not timezone_name.strip():
        raise AthleteEvidenceError("timezone must be a non-empty string")
    moment = now if now is not None else dt.datetime.now(dt.timezone.utc)
    return moment.astimezone(_zone(timezone_name)).date()


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
        "profile": None,
        "availability": {"recurring": None, "week_overrides": []},
        "strength_reports": [],
        "body_measurements": [],
        "reported_activities": [],
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
    # The version does not move for the profile. It is one more optional container beside
    # the two already here, and a checkout that has never heard of it reads the file it
    # always read; making the number move would refuse the whole file -- availability and
    # every reported lift with it -- to code that only lacks this one key.
    profile = value.get("profile")
    if profile is not None and not isinstance(profile, dict):
        raise _unreadable("profile must be an object or null")
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
    # Absent reads as empty, and the version does not move for either of them -- the same
    # decision the profile above records, for the same reason. A file written before these
    # two groups existed is not a damaged file; it is a file from an athlete who had not
    # reported a measurement or an unrecorded session, which is what an empty list says.
    # Making the number move would refuse that whole file -- availability, profile and
    # every reported lift with it -- to a checkout that only lacks these keys.
    measurements = _record_list(value, "body_measurements")
    activities = _record_list(value, "reported_activities")
    return {
        "athlete_evidence_version": ATHLETE_EVIDENCE_VERSION,
        "profile": profile,
        "availability": {"recurring": recurring, "week_overrides": list(overrides)},
        "strength_reports": list(reports),
        "body_measurements": measurements,
        "reported_activities": activities,
    }


def _record_list(value: dict[str, Any], key: str) -> list[dict[str, Any]]:
    """One optional array-of-records container, absent reading as empty.

    Absent and empty are the same fact here -- nothing reported -- so there is nothing to
    distinguish and no reason to refuse an older file. Present-but-not-an-array is a
    different thing entirely and raises, exactly as the required containers above do:
    something wrote a shape this module cannot read, and reading it as "no evidence" would
    drop statements the athlete believes are still on record.
    """
    raw = value.get(key)
    if raw is None:
        return []
    if not isinstance(raw, list) or not all(isinstance(item, dict) for item in raw):
        raise _unreadable(f"{key} must be an array of objects")
    return list(raw)


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
# Profile: where the athlete is, and what language they read
# --------------------------------------------------------------------------------------


def stored_profile(evidence: dict[str, Any]) -> dict[str, Any] | None:
    """The profile this athlete has stated, or ``None`` when they have stated none.

    ``None`` is what tells a coach to ask. It is also what every account carried before
    a profile could be stored, which is why nothing downstream may read it as an error:
    an athlete who has not said where they are is in the ordinary starting state, not a
    broken one.

    A record too damaged to read a field from degrades that field to ``None`` rather than
    the whole profile: half a statement is still a statement, and the missing half is
    reported as missing, which is the same thing an athlete who only said one of them
    produces.
    """
    profile = evidence.get("profile")
    if not isinstance(profile, dict):
        return None
    timezone = profile.get("timezone")
    language = profile.get("language")
    return {
        "timezone": timezone if isinstance(timezone, str) and timezone.strip() else None,
        "language": language if language in LANGUAGES else None,
        "recorded_at": profile.get("recorded_at"),
        "source": profile.get("source"),
    }


def profile_timezone(profile: dict[str, Any] | None) -> str | None:
    """The stated timezone, or ``None`` -- never the default, which is a caller's choice."""
    return (profile or {}).get("timezone")


def profile_language(profile: dict[str, Any] | None) -> str:
    """The language to write this athlete's prescriptions in.

    Falls back to the default here rather than at each caller: a prescription is always
    rendered in *some* language, and leaving every write path to pick the fallback is how
    two of them end up picking differently.
    """
    return (profile or {}).get("language") or DEFAULT_LANGUAGE


def resolve_settings(
    state_dir: Path | str, *, timezone_override: Any = None
) -> tuple[str, str]:
    """The ``(timezone, language)`` one request runs under, read from stored state.

    Precedence for the timezone is this request, then what the athlete stated, then the
    documented default -- so an athlete travelling for a week says so once in that turn
    without disturbing where they live. Language has no override; see the module
    docstring.

    Raises ``AthleteEvidenceError`` for an override that is not an IANA zone, so a
    mistyped timezone is refused rather than quietly falling through to the stored value
    and answering about the wrong day.
    """
    if timezone_override is not None:
        if not isinstance(timezone_override, str) or not timezone_override.strip():
            raise AthleteEvidenceError("timezone must be a non-empty string")
        _zone(timezone_override)
    profile = stored_profile(load_evidence(state_dir))
    timezone = timezone_override or profile_timezone(profile) or DEFAULT_TIMEZONE
    return timezone, profile_language(profile)


def record_profile(
    state_dir: Path | str,
    *,
    timezone: Any = None,
    language: Any = None,
    now: dt.datetime | None = None,
) -> dict[str, Any]:
    """Store where this athlete is, what they read, or both, and answer with what holds.

    Each field is independent and latest-wins. Sending only a language leaves the stored
    timezone exactly where it was: they are two statements that share a record, and an
    athlete correcting one has said nothing about the other.

    The timezone is checked against the system's own IANA database rather than a list kept
    here, because an unknown zone is not a typo to store and interpret later -- every
    "today" this athlete is answered about would be wrong. The language is checked against
    the two the renderer has words for, since storing a third would promise a prescription
    nothing can write.

    No plan needs to exist first. Where an athlete is and what they read are true before
    they have decided what to train, and the first conversation is exactly when they are
    stated.
    """
    if timezone is None and language is None:
        raise AthleteEvidenceError("record_profile needs timezone, language, or both")
    if timezone is not None:
        if not isinstance(timezone, str) or not timezone.strip():
            raise AthleteEvidenceError("timezone must be a non-empty string")
        _zone(timezone)
    if language is not None and language not in LANGUAGES:
        raise AthleteEvidenceError(
            f"language must be one of {', '.join(LANGUAGES)}, found {language!r}"
        )

    recorded_at = _recorded_at(now)
    root = resolve_state_root(state_dir)
    # 0o700 when this module creates it, matching init_store; an already-existing
    # directory keeps whatever the store gave it.
    root.mkdir(parents=True, mode=0o700, exist_ok=True)
    with _exclusive_lock(root, operation="record-profile"):
        # Reported evidence is state like any other: a store handed off to the hosted
        # coach must not accumulate statements the canonical plan will never read.
        _refuse_when_handed_off(root, "record-profile")
        evidence = load_evidence(root)
        held = stored_profile(evidence) or {}
        profile = {
            "timezone": timezone if timezone is not None else held.get("timezone"),
            "language": language if language is not None else held.get("language"),
            "recorded_at": recorded_at,
            "source": ATHLETE_REPORTED_SOURCE,
        }
        evidence["profile"] = profile
        _atomic_json(evidence_path(root), evidence)

    return {
        "athlete_evidence_version": ATHLETE_EVIDENCE_VERSION,
        "profile": profile,
        # What the athlete's day and their prescriptions now run on, including the
        # defaults still standing in for anything they have not stated.
        "effective": {
            "timezone": profile["timezone"] or DEFAULT_TIMEZONE,
            "language": profile_language(profile),
        },
    }


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
    with _exclusive_lock(root, operation="record-availability"):
        _refuse_when_handed_off(root, "record-availability")
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
    parsed_date = _reported_date(date, today=athlete_today(timezone_name, now))

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


def _strength_report_position(
    reports: list[dict[str, Any]], day: str, exercise: str
) -> int | None:
    """Where one movement's record for one day sits, or ``None`` when it holds none.

    The one identity rule a strength record has -- ``(date, exercise_key)`` -- written
    once and read by both sides of the record's life: the upsert that corrects it and
    the retraction that removes it. Two copies of this predicate would eventually let a
    normalization fix land on one side only, and then a retraction reports "not found"
    for a record the next report plainly replaces.
    """
    key = exercise_key(exercise)
    return next(
        (
            index
            for index, item in enumerate(reports)
            if isinstance(item.get("exercise"), str)
            and item.get("date") == day
            and exercise_key(item["exercise"]) == key
        ),
        None,
    )


def _measurement_position(measurements: list[dict[str, Any]], day: str) -> int | None:
    """Where one day's measurement sits, or ``None`` -- date is its whole identity."""
    return next(
        (
            index
            for index, item in enumerate(measurements)
            if item.get("date") == day
        ),
        None,
    )


def _activity_summary_position(
    activities: list[dict[str, Any]], day: str, sport: str
) -> int | None:
    """Where one sport's summary for one day sits, or ``None`` when it holds none."""
    return next(
        (
            index
            for index, item in enumerate(activities)
            if item.get("date") == day and item.get("sport") == sport
        ),
        None,
    )


def _names_on_record(records: list[dict[str, Any]], day: str, field: str) -> list[str]:
    """The distinct ``field`` values already on record for ``day``, sorted for a stable reply.

    Read only when a retraction finds nothing to remove, so the response can say what
    *is* on record instead of just what was not found -- the difference between a dead
    end and a pointer to the name that would have matched.
    """
    return sorted(
        {
            item[field]
            for item in records
            if str(item.get("date")) == day and isinstance(item.get(field), str)
        }
    )


def retract_strength_report(
    state_dir: Path | str,
    *,
    exercise: Any,
    date: Any = None,
    sets: Any = None,
    category: Any = None,
    notes: Any = None,
    timezone_name: str = DEFAULT_TIMEZONE,
    now: dt.datetime | None = None,
) -> dict[str, Any]:
    """Remove a movement's record for one day -- the athlete taking it back, not correcting it.

    Keyed by the same ``(date, exercise)`` identity ``record_strength_report`` upserts
    on, and it removes whatever it finds there regardless of ``source``: a confirmed
    prescription and a set-by-set report are two different ways this fact reached the
    store, and "we didn't actually do that" is true of either one.

    ``sets``, ``category`` and ``notes`` are accepted as parameters only so a retraction
    that also tries to carry them can be refused by name. A retraction states that the
    record should not stand; it cannot also state content, so restating what was really
    lifted is a second call, through ``record_strength_report``.

    A retraction that finds nothing is not an error: the athlete may be recalling a
    report that was never made, or naming the movement differently than it was stored
    under. ``removed`` is ``None``, ``note`` says so in one sentence, and
    ``on_record_that_day`` names whatever this athlete does have on record for that day,
    so a caller can retry with the stored name instead of asking the athlete to repeat
    themselves blind. A second retraction of a record already removed produces exactly
    this same miss, which is what keeps retraction safe to repeat.
    """
    if sets is not None or category is not None or notes is not None:
        raise AthleteEvidenceError(
            "a retraction states the record should not stand; sets, category and notes "
            "belong in a new report instead"
        )
    if not isinstance(exercise, str) or not exercise.strip():
        raise AthleteEvidenceError("exercise must be a non-empty string")
    day = _reported_date(date, today=athlete_today(timezone_name, now)).isoformat()

    root = resolve_state_root(state_dir)
    # 0o700 when this module creates it, matching init_store; an already-existing
    # directory keeps whatever the store gave it.
    root.mkdir(parents=True, mode=0o700, exist_ok=True)
    with _exclusive_lock(root, operation="retracting a reported strength record"):
        _refuse_when_handed_off(root, "retracting a reported strength record")
        evidence = load_evidence(root)
        reports = evidence["strength_reports"]
        position = _strength_report_position(reports, day, exercise)
        if position is None:
            on_record = _names_on_record(reports, day, "exercise")
            miss_note = f"no strength record for {exercise!r} on {day} was found to retract"
            if on_record:
                miss_note += f"; on record for that day: {', '.join(on_record)}"
            return {
                "retracted": True,
                "removed": None,
                "report_count": len(reports),
                "on_record_that_day": on_record,
                "note": miss_note,
            }
        removed = reports.pop(position)
        _atomic_json(evidence_path(root), evidence)
        return {
            "retracted": True,
            "removed": removed,
            "report_count": len(reports),
            "on_record_that_day": None,
            "note": None,
        }


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
    with _exclusive_lock(root, operation="recording reported strength"):
        _refuse_when_handed_off(root, "recording reported strength")
        evidence = load_evidence(root)
        reports = evidence["strength_reports"]
        changed = False
        for content in contents:
            report_id = canonical_hash(content)
            position = _strength_report_position(
                reports, content["date"], content["exercise"]
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


# --------------------------------------------------------------------------------------
# Body measurements
# --------------------------------------------------------------------------------------


def _bounded_number(value: Any, field: str) -> float | int | None:
    """One measured figure inside the range a reading of that instrument can fall in."""
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise AthleteEvidenceError(f"{field} must be a number or null")
    low, high = BODY_MEASUREMENT_BOUNDS[field]
    if not low <= float(value) <= high:
        raise AthleteEvidenceError(
            f"{field} must be between {low:g} and {high:g}, found {value!r}"
        )
    return value


def record_body_measurement(
    state_dir: Path | str,
    *,
    weight_kg: Any = None,
    body_fat_pct: Any = None,
    date: Any = None,
    timezone_name: str = DEFAULT_TIMEZONE,
    now: dt.datetime | None = None,
) -> dict[str, Any]:
    """Store what the athlete weighed, or measured, on one day.

    At least one figure is required, and each is independent: sending a body fat
    percentage leaves the day's weight exactly where it was, the same way stating a
    language leaves a stored timezone alone. They are two readings that share a day, not
    one record with two halves, and an athlete who stepped on a scale has not thereby
    measured their composition.

    **One record per day, and the newest statement wins.** "72.5, sorry, 72.3" is one
    weigh-in stated twice; a store that appended it would show the coach a kilogram of
    movement that never happened. The response names what the restatement displaced.

    Nothing is derived. No trend, no rate of change, no comparison against a target or a
    previous week -- the series is handed to the coach raw, because what a kilogram means
    inside a hybrid block depends on the training that produced it and a number computed
    here would have made that reading first (AGENTS.md 4, 5).

    The date may not be in the athlete's future, and each figure is refused outside the
    range a scale can produce at all; see ``BODY_MEASUREMENT_BOUNDS`` for why refusing
    beats storing. No plan needs to exist first.
    """
    if weight_kg is None and body_fat_pct is None:
        raise AthleteEvidenceError(
            "record_body_measurement needs weight_kg, body_fat_pct, or both"
        )
    parsed_date = _reported_date(date, today=athlete_today(timezone_name, now))
    stated = {
        name: _bounded_number(value, name)
        for name, value in (("weight_kg", weight_kg), ("body_fat_pct", body_fat_pct))
    }

    recorded_at = _recorded_at(now)
    root = resolve_state_root(state_dir)
    # 0o700 when this module creates it, matching init_store; an already-existing
    # directory keeps whatever the store gave it.
    root.mkdir(parents=True, mode=0o700, exist_ok=True)
    with _exclusive_lock(root, operation="recording a body measurement"):
        _refuse_when_handed_off(root, "recording a body measurement")
        evidence = load_evidence(root)
        measurements = evidence["body_measurements"]
        day = parsed_date.isoformat()
        position = _measurement_position(measurements, day)
        held = measurements[position] if position is not None else {}
        content = {
            "date": day,
            **{
                name: stated[name] if stated[name] is not None else held.get(name)
                for name in BODY_MEASUREMENT_VALUES
            },
        }
        measurement_id = canonical_hash(content)
        if position is not None and held.get("measurement_id") == measurement_id:
            return {
                "measurement_id": measurement_id,
                "idempotent_replay": True,
                "replaced": None,
                "measurement": held,
                "measurement_count": len(measurements),
            }
        measurement = {
            "measurement_id": measurement_id,
            **content,
            "recorded_at": recorded_at,
            "source": ATHLETE_REPORTED_SOURCE,
        }
        replaced: dict[str, Any] | None = None
        if position is None:
            measurements.append(measurement)
        else:
            # Returned, never kept. Two readings for one day is the arithmetic problem
            # this rule exists to prevent, and the athlete can see what their correction
            # displaced without the coach ever holding both.
            replaced = held
            measurements[position] = measurement
        _atomic_json(evidence_path(root), evidence)
        return {
            "measurement_id": measurement_id,
            "idempotent_replay": False,
            "replaced": replaced,
            "measurement": measurement,
            "measurement_count": len(measurements),
        }


def retract_body_measurement(
    state_dir: Path | str,
    *,
    date: Any = None,
    weight_kg: Any = None,
    body_fat_pct: Any = None,
    timezone_name: str = DEFAULT_TIMEZONE,
    now: dt.datetime | None = None,
) -> dict[str, Any]:
    """Remove one day's whole measurement record -- the athlete taking it back, not correcting it.

    Keyed by ``date`` alone, because that is the whole of a measurement's identity: unlike
    a lift or a reported session, there is no second name to get wrong. ``weight_kg`` and
    ``body_fat_pct`` are accepted as parameters only so a retraction that also tries to
    carry one is refused by name -- a retraction says the day's reading should not stand,
    and cannot also state one.

    The whole day is removed, not one figure of it, because the two live in a single
    record. An athlete who meant only the weight was wrong keeps the day's body fat by
    restating it right after, through ``record_body_measurement`` -- the removed record
    is echoed back in full for exactly that: reading off the half that was still right
    and sending it straight back.

    A retraction that finds nothing is not an error: ``removed`` is ``None`` and ``note``
    says so in one sentence. A second retraction of a record already removed produces
    exactly this same miss, which is what keeps retraction safe to repeat.
    """
    if weight_kg is not None or body_fat_pct is not None:
        raise AthleteEvidenceError(
            "a retraction states the day's record should not stand; weight_kg and "
            "body_fat_pct belong in a new measurement instead"
        )
    day = _reported_date(date, today=athlete_today(timezone_name, now)).isoformat()

    root = resolve_state_root(state_dir)
    # 0o700 when this module creates it, matching init_store; an already-existing
    # directory keeps whatever the store gave it.
    root.mkdir(parents=True, mode=0o700, exist_ok=True)
    with _exclusive_lock(root, operation="retracting a body measurement"):
        _refuse_when_handed_off(root, "retracting a body measurement")
        evidence = load_evidence(root)
        measurements = evidence["body_measurements"]
        position = _measurement_position(measurements, day)
        if position is None:
            return {
                "retracted": True,
                "removed": None,
                "measurement_count": len(measurements),
                "note": f"no body measurement for {day} was found to retract",
            }
        removed = measurements.pop(position)
        _atomic_json(evidence_path(root), evidence)
        return {
            "retracted": True,
            "removed": removed,
            "measurement_count": len(measurements),
            "note": None,
        }


def body_measurement_series(
    evidence: dict[str, Any], window: BuildWindow
) -> list[dict[str, Any]]:
    """The measurements inside ``window``, newest day first, exactly as stated.

    One row per day by construction -- ``record_body_measurement`` replaces rather than
    appends -- so nothing is averaged, deduped or interpolated here. A record too damaged
    to place on a date is skipped rather than allowed to fail the whole build; a day
    holding only one of the two figures is not damage, it is the ordinary case.
    """
    series: list[dict[str, Any]] = []
    for record in evidence.get("body_measurements") or []:
        if not isinstance(record, dict):
            continue
        try:
            day = dt.date.fromisoformat(str(record.get("date")))
        except ValueError:
            continue
        if not (window.window42_start <= day <= window.window42_end):
            continue
        values = {
            name: record.get(name)
            if isinstance(record.get(name), (int, float))
            and not isinstance(record.get(name), bool)
            else None
            for name in BODY_MEASUREMENT_VALUES
        }
        if all(value is None for value in values.values()):
            continue
        series.append({"date": day.isoformat(), **values, "source": ATHLETE_REPORTED_SOURCE})
    series.sort(key=lambda item: item["date"], reverse=True)
    return series


# --------------------------------------------------------------------------------------
# Sessions no device recorded
# --------------------------------------------------------------------------------------


def _positive_number(value: Any, field: str, *, integer: bool) -> Any:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int if integer else (int, float)):
        kind = "an integer" if integer else "a number"
        raise AthleteEvidenceError(f"{field} must be {kind} or null")
    if value <= 0:
        raise AthleteEvidenceError(f"{field} must be greater than 0, found {value!r}")
    return value


def record_activity_summary(
    state_dir: Path | str,
    *,
    sport: Any,
    duration_minutes: Any,
    date: Any = None,
    distance_km: Any = None,
    subjective_feel: Any = None,
    note: Any = None,
    timezone_name: str = DEFAULT_TIMEZONE,
    now: dt.datetime | None = None,
) -> dict[str, Any]:
    """Store a session the athlete trained and no device recorded.

    ``sport`` and ``duration_minutes`` are the two facts a session cannot be described
    without, and they are the only required ones: "我今天游了 40 分鐘" is a complete
    statement, while a route asking for distance would turn it into a form. Everything
    else is taken when the athlete volunteers it and left null when they do not.
    ``subjective_feel`` is the same 1-5 scale ``recent_actuals`` already carries, so a
    reported session and a recorded one describe effort in one vocabulary.

    **This is not an actual, and nothing here makes it one.** It gets no activity id, it
    never enters ``recent_actuals``, it completes no planned session, and reconciliation
    never reads it. A session counted as both a report and a provider activity would be
    one week's training read as two, and the product's claim about what came back would
    stop being about what the provider actually holds (AGENTS.md 8). The coach sees it
    beside provider evidence, labelled ``athlete_reported``, and weighs it as the
    athlete's word.

    **One summary per sport per day, and the newest wins.** Restating corrects: "40 分鐘，
    啊是 45" is one session described twice. Version 1 cannot hold two genuinely distinct
    sessions of one sport on one day -- the response names what a restatement displaced,
    so a second one is never lost quietly, and an athlete who really ran twice is better
    served by one combined summary than by a disambiguation question.

    The date may not be in the athlete's future. Nothing is scored, compared against the
    plan, or converted into a pace: what 45 minutes of running that week means is the
    coach's judgment.
    """
    if not isinstance(sport, str) or sport.strip().lower() not in REPORTABLE_SPORTS:
        raise AthleteEvidenceError(
            f"sport must be one of {', '.join(REPORTABLE_SPORTS)}, found {sport!r}"
        )
    parsed_sport = sport.strip().lower()
    parsed_date = _reported_date(date, today=athlete_today(timezone_name, now))
    minutes = _positive_number(duration_minutes, "duration_minutes", integer=True)
    if minutes is None:
        raise AthleteEvidenceError("duration_minutes is required")
    distance = _positive_number(distance_km, "distance_km", integer=False)
    if subjective_feel is not None and (
        isinstance(subjective_feel, bool)
        or not isinstance(subjective_feel, int)
        or not 1 <= subjective_feel <= 5
    ):
        raise AthleteEvidenceError(
            f"subjective_feel must be an integer from 1 to 5 or null, found {subjective_feel!r}"
        )
    if note is not None and (not isinstance(note, str) or not note.strip()):
        raise AthleteEvidenceError("note must be a non-empty string or null")

    content = {
        "date": parsed_date.isoformat(),
        "sport": parsed_sport,
        "duration_minutes": minutes,
        "distance_km": distance,
        "subjective_feel": subjective_feel,
        "note": note,
    }
    summary_id = canonical_hash(content)

    recorded_at = _recorded_at(now)
    root = resolve_state_root(state_dir)
    # 0o700 when this module creates it, matching init_store; an already-existing
    # directory keeps whatever the store gave it.
    root.mkdir(parents=True, mode=0o700, exist_ok=True)
    with _exclusive_lock(root, operation="recording a reported activity"):
        _refuse_when_handed_off(root, "recording a reported activity")
        evidence = load_evidence(root)
        activities = evidence["reported_activities"]
        position = _activity_summary_position(activities, content["date"], parsed_sport)
        if position is not None and activities[position].get("summary_id") == summary_id:
            return {
                "summary_id": summary_id,
                "idempotent_replay": True,
                "replaced": None,
                "activity": activities[position],
                "activity_count": len(activities),
            }
        summary = {
            "summary_id": summary_id,
            **content,
            "recorded_at": recorded_at,
            "source": ATHLETE_REPORTED_SOURCE,
        }
        replaced: dict[str, Any] | None = None
        if position is None:
            activities.append(summary)
        else:
            replaced = activities[position]
            activities[position] = summary
        _atomic_json(evidence_path(root), evidence)
        return {
            "summary_id": summary_id,
            "idempotent_replay": False,
            "replaced": replaced,
            "activity": summary,
            "activity_count": len(activities),
            # Said only when something was displaced, and said plainly, because this is
            # the one place the version 1 limitation can actually bite: a caller that
            # meant to add a second session has just overwritten the first, and it can
            # see so here rather than after the athlete notices a missing run.
            "replaced_note": (
                None
                if replaced is None
                else (
                    f"a {parsed_sport} summary for {content['date']} was already on "
                    "record and has been replaced; one summary per sport per day is "
                    "held, so two genuinely distinct sessions belong in one combined "
                    "summary"
                )
            ),
        }


def retract_activity_summary(
    state_dir: Path | str,
    *,
    sport: Any,
    date: Any = None,
    duration_minutes: Any = None,
    distance_km: Any = None,
    subjective_feel: Any = None,
    note: Any = None,
    timezone_name: str = DEFAULT_TIMEZONE,
    now: dt.datetime | None = None,
) -> dict[str, Any]:
    """Remove a sport's reported session for one day -- the athlete taking it back, not correcting it.

    Keyed by the same ``(date, sport)`` identity ``record_activity_summary`` upserts on.
    ``duration_minutes``, ``distance_km``, ``subjective_feel`` and ``note`` are accepted
    as parameters only so a retraction that also tries to carry one is refused by name: a
    retraction says the session should not stand, and cannot also describe one -- that is
    a second statement, made through ``record_activity_summary``.

    A retraction that finds nothing is not an error, for the same reason a miss is not an
    error anywhere else in this module: the athlete may be recalling a session that was
    never reported, or naming a different sport than it was reported under.
    ``on_record_that_day`` names the sports this athlete does have reported for that day,
    so a caller can tell the two apart and retry with the right one. A second retraction
    of a summary already removed produces exactly this same miss, which is what keeps
    retraction safe to repeat.
    """
    if (
        duration_minutes is not None
        or distance_km is not None
        or subjective_feel is not None
        or note is not None
    ):
        raise AthleteEvidenceError(
            "a retraction states the session should not stand; duration_minutes, "
            "distance_km, subjective_feel and note belong in a new summary instead"
        )
    if not isinstance(sport, str) or sport.strip().lower() not in REPORTABLE_SPORTS:
        raise AthleteEvidenceError(
            f"sport must be one of {', '.join(REPORTABLE_SPORTS)}, found {sport!r}"
        )
    parsed_sport = sport.strip().lower()
    day = _reported_date(date, today=athlete_today(timezone_name, now)).isoformat()

    root = resolve_state_root(state_dir)
    # 0o700 when this module creates it, matching init_store; an already-existing
    # directory keeps whatever the store gave it.
    root.mkdir(parents=True, mode=0o700, exist_ok=True)
    with _exclusive_lock(root, operation="retracting a reported activity"):
        _refuse_when_handed_off(root, "retracting a reported activity")
        evidence = load_evidence(root)
        activities = evidence["reported_activities"]
        position = _activity_summary_position(activities, day, parsed_sport)
        if position is None:
            on_record = _names_on_record(activities, day, "sport")
            miss_note = f"no {parsed_sport} summary for {day} was found to retract"
            if on_record:
                miss_note += f"; on record for that day: {', '.join(on_record)}"
            return {
                "retracted": True,
                "removed": None,
                "activity_count": len(activities),
                "on_record_that_day": on_record,
                "note": miss_note,
            }
        removed = activities.pop(position)
        _atomic_json(evidence_path(root), evidence)
        return {
            "retracted": True,
            "removed": removed,
            "activity_count": len(activities),
            "on_record_that_day": None,
            "note": None,
        }


def reported_activity_summaries(
    evidence: dict[str, Any], window: BuildWindow
) -> list[dict[str, Any]]:
    """The reported sessions inside ``window``, newest day first, exactly as stated.

    Deliberately shaped like a summary and not like an actual: no activity id, no
    ``match_confidence``, no ``completion``, nothing an attachment could be built from.
    The coach reads these beside ``recent_actuals``, never inside it.

    One row per (date, sport) by construction. A record too damaged to place on a date or
    naming no known sport is skipped rather than allowed to fail the whole build.
    """
    summaries: list[dict[str, Any]] = []
    for record in evidence.get("reported_activities") or []:
        if not isinstance(record, dict):
            continue
        try:
            day = dt.date.fromisoformat(str(record.get("date")))
        except ValueError:
            continue
        if not (window.window42_start <= day <= window.window42_end):
            continue
        if record.get("sport") not in REPORTABLE_SPORTS:
            continue
        summaries.append(
            {
                **{name: record.get(name) for name in _ACTIVITY_SUMMARY_FIELDS},
                "date": day.isoformat(),
                "source": ATHLETE_REPORTED_SOURCE,
            }
        )
    summaries.sort(key=lambda item: (item["date"], item["sport"]))
    summaries.sort(key=lambda item: item["date"], reverse=True)
    return summaries
