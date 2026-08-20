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
- **What they are training for beyond this cycle, and how they like to train.** A cycle
  is 28 days. "I want VO2max at 50, and 80 kg" and "Sunday is my long run" outlive it,
  and a PlanState carrying them would lose both the moment the cycle closed -- so they
  sit here, where availability and the profile already survive one. Two containers, not
  one, because they are answered differently. A **long-term goal** is what the athlete is
  aiming at; the cycle's own ``goal`` is the milestone the coach picks on the way to it,
  and is never a second copy of the target. A **training preference** is a habit the
  coach starts from and may argue with: Friday quality runs, five strength sessions,
  chest-back-legs.

  Neither is a constraint. Nothing here refuses a plan for disagreeing with a preference,
  because the week the athlete is used to and the week that trains them best are two
  different things, and a validator that fused them would have made the coaching judgment
  for the coach (AGENTS.md 5). What the coach owes a preference it departs from is the
  reason, not obedience.

  A preference is what the athlete *said*, never what the history *shows*. Three weeks of
  three strength sessions against a stated five is a divergence to raise with them; it is
  not the store quietly rewriting the five, because a preference the product edited on
  their behalf has stopped being their statement and nobody would ever be told it moved.
  The same rule keeps an inferred habit -- "you usually long-run on Sunday" -- out of this
  file entirely until the athlete says it themselves. The history is already in the
  context for the coach to reason from; copying an inference in here would turn a reading
  into a record (issue #163).

  What expires instead is the week. A trip, a closed gym, a work week that ate Friday are
  statements about *this* week, and they ride the availability week statement -- a
  ``note`` beside the days it already carries -- so the standing preference survives them
  untouched and they vanish when the week does. That is why there is no separate
  temporary-constraint store: availability already holds exactly one recurring layer and
  one week layer, and a second pair of them would be two answers to "what does this week
  look like".
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

- **How the athlete says they feel.** "我覺得很累", "最近睡不好". Until now a subjective
  statement was transient: the coach weighed it fully inside the conversation it was said
  in, and what survived was only the plan change it caused -- a reason code, a sentence in
  a summary. So three weeks of "very tired" was three separate turns and never a *pattern*,
  which is the reading with the most coaching value in it and the one nothing could see
  (issue #188).

  Stored as the athlete's own sentence with the day it is about, and nothing else. Not
  scored, not translated into a number, not turned into a reason code, and read by no
  validator: the standing ban on subjective feeling entering ``recovery_signals`` as a
  figure is exactly right and is untouched by this, because this stores the sentence
  rather than a reading of it. What "tired for three weeks" means -- a month to ease, a
  recovery problem worth naming, or a fortnight of bad sleep that has already passed -- is
  the coach's judgment over the notes and their dates (AGENTS.md 4, 5).

  It is **not** the symptom channel. Pain, illness, chest pain, dizziness and unusual
  symptoms are ``red_flags`` on the session request, where an explicit true limits the day
  to rest deterministically (AGENTS.md 9). A symptom written here would be a sentence the
  coach may read and nothing would act on, which is the one failure this product may not
  have. The two tool descriptions say so on both sides of the line.

  One note per day, newest winning, for the reason every other dated record here has that
  rule: "很累，其實是沒睡好" is one state described twice, and a store that appended it
  would show the coach two days' worth of complaint from one afternoon. The response names
  what a restatement displaced, so a second statement is never lost quietly, and a day
  that genuinely holds two things is one sentence holding both.

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
import re
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
    "LONG_TERM_GOAL_FIELDS",
    "PRESCRIBED_CONFIRMED_SOURCE",
    "PROFILE_FIELDS",
    "REPORTABLE_SPORTS",
    "SUBJECTIVE_STATE_FIELDS",
    "SUBJECTIVE_STATE_MAX_CHARS",
    "SUBJECTIVE_STATE_WINDOW_DAYS",
    "TRAINING_PREFERENCE_FIELDS",
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
    "record_long_term_goal",
    "record_profile",
    "record_strength_report",
    "record_subjective_state",
    "record_training_preference",
    "reported_activity_summaries",
    "reported_strength_sessions",
    "reported_subjective_states",
    "resolve_settings",
    "retract_activity_summary",
    "retract_body_measurement",
    "retract_long_term_goal",
    "retract_strength_report",
    "retract_subjective_state",
    "retract_training_preference",
    "stated_long_term_goals",
    "stated_training_preferences",
    "statement_key",
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

# The provenance of a session that arrived in a file the athlete uploaded rather than in a
# sentence they said. Still the athlete's own evidence, still never a provider actual --
# the coach reads it beside ``recent_actuals`` exactly as a spoken report is read -- but
# "I swam 40 minutes" and "my watch's export says 43:12" are different claims, and a coach
# weighing a progression needs to see which one it has.
ATHLETE_IMPORTED_SOURCE = "athlete_imported"

# The one sameness rule in this module, and the reason an import cannot double-count.
# Two sessions are the same session when they share a day and a sport and their durations
# agree this closely. Three minutes is wide enough to absorb elapsed-versus-moving time,
# which is the usual reason one session is described two ways, and narrow enough that a
# 30-minute jog and a 60-minute easy run on one evening stay two sessions.
#
# It is asked from both directions, which is what keeps one answer: an import row asks it
# of what is already stored, and a spoken summary asks it of a day an import already
# covers. Neither side has a rule of its own.
SAME_SESSION_MINUTES = 3

# The same question asked of distance, when *both* records state one. A session whose
# distance disagrees by more than this is a different session however close its duration
# lands: 8 km and 15 km in 45 minutes are not one run described twice. One side missing a
# distance decides nothing -- a Garmin export with no unit and an athlete who said only
# "45 minutes" both leave it null, and neither absence is evidence of a second session.
SAME_SESSION_DISTANCE_KM = 1.0

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
_WEEK_FIELDS = (*_AVAILABILITY_DAY_FIELDS, "only_days", "week_start", "note")

# One long-term goal: what the athlete is aiming at past the end of any one cycle.
# ``target`` is text on purpose. "50", "80 kg", "sub-25:00" and "a bodyweight pull-up" are
# one kind of sentence to the athlete and four schemas to a product that parsed them, and
# the comparison against where they stand today is the coach's reading of evidence this
# file does not hold (AGENTS.md 4). Nothing here records a *current* value: a target is
# intent, and the measurement of it belongs to the provider and to the athlete's own
# reported measurements, never to a second copy kept beside them (issue #163).
LONG_TERM_GOAL_FIELDS = ("metric", "target", "target_date", "note", "recorded_at", "source")

# One training habit, in the athlete's own words. No frequency field, no weekday field, no
# structure at all -- the moment a habit is machine-readable something starts checking
# plans against it, and a preference that can fail a plan has become a constraint.
TRAINING_PREFERENCE_FIELDS = ("topic", "statement", "recorded_at", "source")

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

# One day's subjective state: the athlete's own sentence and the day it is about. No
# scale, no score, no category -- a subjective state that is machine-readable is one
# something starts computing with, and the whole reason this exists is that a number
# standing in for "很累" was the thing the product already refused to store.
SUBJECTIVE_STATE_FIELDS = ("date", "note", "recorded_at", "source")

# How long one note may be. A cap rather than no cap, because this file is read into every
# session context and an unbounded field is an unbounded context (AGENTS.md 13); generous
# rather than tight, because the value here is the athlete's own wording and a rule that
# makes them edit it has already lost what it was storing. A statement past this is refused
# with the length named, never truncated: half a sentence read as the whole of one is worse
# than a refusal the athlete can answer.
SUBJECTIVE_STATE_MAX_CHARS = 600

# How far back the session context carries these. Fourteen days is two natural weeks, which
# is the shortest span in which "three weeks of this" starts to be visible at all and the
# same window `recent_actuals` already reads -- so a note and the training it sits beside
# are read over one frame rather than two. Older notes stay in the store and in an export;
# they simply stop being handed to every turn.
SUBJECTIVE_STATE_WINDOW_DAYS = 14


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
        "long_term_goals": [],
        "training_preferences": [],
        "strength_reports": [],
        "body_measurements": [],
        "reported_activities": [],
        "subjective_states": [],
        "imports": [],
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
    # The same terms again, and the same reason: a file written before an athlete could
    # say how they feel is a file from an athlete who has not said it, not a damaged one.
    states = _record_list(value, "subjective_states")
    # The ledger of uploads, on the same terms: absent reads as empty and the version does
    # not move. It holds no file content -- a digest, a format, a count and a timestamp --
    # and exists so that dragging the same export in twice is visibly the same upload
    # rather than a second copy of a year of training.
    imports = _record_list(value, "imports")
    # The two standing statements, on the same terms again: what the athlete is training
    # for past this cycle, and how they say they like to train. An athlete who has stated
    # neither is an ordinary athlete, not a damaged file.
    goals = _record_list(value, "long_term_goals")
    preferences = _record_list(value, "training_preferences")
    return {
        "athlete_evidence_version": ATHLETE_EVIDENCE_VERSION,
        "profile": profile,
        "availability": {"recurring": recurring, "week_overrides": list(overrides)},
        "long_term_goals": goals,
        "training_preferences": preferences,
        "strength_reports": list(reports),
        "body_measurements": measurements,
        "reported_activities": activities,
        "subjective_states": states,
        "imports": imports,
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


# The fields that make one recurring week the same statement as another: the days the
# athlete named, and nothing the store stamps on beside them.
_RECURRING_CONTENT = ("available_days", "unavailable_days")


def _same_statement(
    stored: dict[str, Any], stated: dict[str, Any], content: tuple[str, ...]
) -> bool:
    """Whether a record already on file says what a statement about to be written says.

    ``content`` names the fields that are the athlete's words. Everything else on a
    stored record -- ``recorded_at``, ``source`` -- is provenance of the write that put
    it there, and two writes of one statement differ in exactly that, which is why
    provenance cannot be part of the comparison. Both availability halves ask this
    question, and they ask it the same way on purpose: "the same statement" is one
    judgement, not one per field shape.
    """
    return canonical_hash({key: stored.get(key) for key in content}) == canonical_hash(
        {key: stated.get(key) for key in content}
    )


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


def _week_note(value: Any) -> str | None:
    """The one thing about this week that is not a weekday, or ``None``.

    A trip, a hotel gym with nothing but dumbbells, a work week that will run late: real
    constraints on this week that name no lost day, and that a coach planning it needs.
    Free text, because the alternative is an equipment vocabulary and a travel flag, and
    the next athlete states something neither of them has a field for. It is scoped to
    the week statement it rides on and expires with it -- which is the entire reason a
    temporary constraint does not go anywhere near a standing preference.
    """
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise AthleteEvidenceError("week.note must be a non-empty string or null")
    return value.strip()


def _week_statement(value: dict[str, Any], *, today: dt.date) -> dict[str, Any]:
    """Validate one statement about a single week, in either of its two forms.

    The forms are mutually exclusive because they answer different questions. ``only_days``
    says what the whole week is; ``available_days``/``unavailable_days`` say what changed
    about it. Accepting both at once would leave the week's meaning to evaluation order.

    A ``note`` may ride either form, or stand alone. Standing alone is not an empty
    statement: "I'm travelling this week, hotel gym only" costs the athlete no training
    day and is exactly the sentence a week plan has to be built around.
    """
    unexpected = sorted(set(value) - set(_WEEK_FIELDS))
    if unexpected:
        raise AthleteEvidenceError(f"week does not accept {', '.join(unexpected)}")
    week_start = _week_start(value.get("week_start"), today=today)
    note = _week_note(value.get("note"))
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
            "note": note,
        }
    if not changes:
        if note is None:
            raise AthleteEvidenceError("week must name at least one weekday or carry a note")
        return {
            "week_start": week_start.isoformat(),
            "only_days": None,
            "available_days": [],
            "unavailable_days": [],
            "note": note,
        }
    available, unavailable = _day_lists(
        {key: value.get(key) for key in _AVAILABILITY_DAY_FIELDS}, "week"
    )
    return {
        "week_start": week_start.isoformat(),
        "only_days": None,
        "available_days": available,
        "unavailable_days": unavailable,
        "note": note,
    }


# The fields that make one week statement the same statement as another: everything
# `_week_statement` returns and nothing the store stamps on -- `recorded_at` and
# `source` are provenance of a write, not part of what the athlete said.
_WEEK_STATEMENT_CONTENT = (
    "week_start",
    "only_days",
    "available_days",
    "unavailable_days",
    "note",
)


def _standing_week_statement(overrides: list[Any], week_start: str) -> dict[str, Any] | None:
    """The statement currently standing for ``week_start``: the last one made about it.

    Last by position, which is last by time: the list is append-only, the same ground
    ``effective_availability`` stands on when it breaks ``recorded_at`` ties by position.
    """
    for item in reversed(overrides):
        if isinstance(item, dict) and item.get("week_start") == week_start:
            return item
    return None


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

    Both halves recognise a replay, which is what lets a client retry this call after a
    timeout. A ``week`` statement identical to the one standing for its week is answered
    from the record rather than layered again, so the same note cannot reach the coach
    twice in ``week_constraints``; a ``recurring`` value identical to the one on record
    is left standing rather than re-stamped with a fresh ``recorded_at``. Sameness is
    what the athlete said and not when they said it -- ``_same_statement``.
    ``idempotent_replay`` says every half that was sent was recognised that way, which is
    also the condition under which the file is never opened for writing.

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
    wrote_recurring = False
    wrote_week = False
    with _exclusive_lock(root, operation="record-availability"):
        _refuse_when_handed_off(root, "record-availability")
        evidence = load_evidence(root)
        if recurring_days is not None:
            record = _availability_record(*recurring_days, recorded_at=recorded_at)
            standing_recurring = evidence["availability"]["recurring"]
            # Latest-wins, but a replay is not a later statement. Overwriting the record
            # with an identical one moves no day, and it re-dates the schedule -- so a
            # client retrying a timeout would age a week the athlete never touched, and
            # a reader asking when they last said this would get the retry's clock.
            # Leaving the standing record alone is the stricter no-op.
            if standing_recurring is None or not _same_statement(
                standing_recurring, record, _RECURRING_CONTENT
            ):
                evidence["availability"]["recurring"] = record
                wrote_recurring = True
        if statement is not None:
            standing = _standing_week_statement(
                evidence["availability"]["week_overrides"], statement["week_start"]
            )
            if standing is not None and _same_statement(
                standing, statement, _WEEK_STATEMENT_CONTENT
            ):
                # The replay check every other record writer makes. Layering an
                # identical repeat would move no day, but its note would reach the
                # coach twice in ``week_constraints`` -- which is what a client
                # retrying a timeout would do. Only the week's *standing* statement
                # is compared: repeating an earlier one after something else changed
                # (only_days again, after a day was taken back) is a real
                # restatement and must compose.
                recorded_week = standing
            else:
                recorded_week = {
                    **statement,
                    "recorded_at": recorded_at,
                    "source": ATHLETE_REPORTED_SOURCE,
                }
                evidence["availability"]["week_overrides"].append(recorded_week)
                wrote_week = True
        if wrote_recurring or wrote_week:
            _atomic_json(evidence_path(root), evidence)

    return {
        "athlete_evidence_version": ATHLETE_EVIDENCE_VERSION,
        # True when every half the caller sent was already on record, which is exactly
        # when the block above opened the file for nothing and skipped the write: the
        # answer below is what was already stored, down to each ``recorded_at``.
        "idempotent_replay": not (wrote_recurring or wrote_week),
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

    Every statement's ``note`` collects into ``week_constraints``, in the order the
    athlete said them, and none of them is interpreted here: what a closed hotel gym does
    to a leg day is the coach's reading. A statement carrying only a note leaves the days
    and ``basis`` exactly as they were, because it changed no day -- reporting it as an
    adjustment would say the athlete moved a day they never mentioned.

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

    notes: list[str] = []
    for _, _, item in statements:
        note = item.get("note")
        if isinstance(note, str) and note.strip():
            notes.append(note.strip())
        only_days = item.get("only_days")
        if not (isinstance(only_days, list) and only_days) and not (
            item.get("available_days") or item.get("unavailable_days")
        ):
            # A note-only statement. It belongs to this week and is already collected;
            # it moves no day, so it must not claim to have adjusted one.
            continue
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

    if basis is None and not notes:
        return None
    return {
        "week_start": target,
        "available_days": available,
        "unavailable_days": unavailable,
        # Never empty-as-unknown: an athlete who stated no constraint and one whose
        # constraints all belong to another week are the same fact, which is none.
        "week_constraints": notes,
        # ``None`` here means nobody has stated a training day -- which stays true when a
        # note about this week is the only thing on record.
        "basis": basis,
        "recorded_at": recorded_at,
        "source": source,
    }


# --------------------------------------------------------------------------------------
# What the athlete is training for, and how they like to train
# --------------------------------------------------------------------------------------


def statement_key(value: str) -> str:
    """The identity two statements about one goal, or one habit, share.

    Case, punctuation **and whitespace** all fold, so "VO2max", "VO2 max", "vo2-max" and
    "VO2 Max" are one goal. This is deliberately looser than ``exercise_key``, which keeps
    the space, and the difference is a difference in what a wrong answer costs. A movement
    list holds dozens of entries and two of them really can differ only by a space, so
    merging there loses a lift the athlete keeps apart. An athlete holds a handful of
    long-term goals and a handful of habits, all of them read back in full in every
    response and every context; two records for one target is the failure that actually
    happens there, and it is the one that leaves the coach unable to say which is current.

    It still stops well short of meaning: no synonym table, no stemming, no vocabulary of
    metrics. "體重" and "body weight" stay two records, and so do "重訓次數" and
    "每週重訓頻率" -- guessing those are one is a judgment, and this is a key.
    """
    return "".join(re.findall(r"[^\W_]+", str(value).lower(), re.UNICODE))


def _standing_text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise AthleteEvidenceError(f"{field} must be a non-empty string")
    return value.strip()


def _standing_position(records: list[dict[str, Any]], key_field: str, key: str) -> int | None:
    for index, record in enumerate(records):
        value = record.get(key_field)
        if isinstance(value, str) and statement_key(value) == key:
            return index
    return None


def _standing_names(records: list[dict[str, Any]], key_field: str) -> list[str]:
    return [
        record[key_field]
        for record in records
        if isinstance(record.get(key_field), str) and record[key_field].strip()
    ]


def _upsert_standing(
    state_dir: Path | str,
    *,
    container: str,
    key_field: str,
    content: dict[str, Any],
    operation: str,
    now: dt.datetime | None,
) -> tuple[dict[str, Any], dict[str, Any] | None, list[dict[str, Any]]]:
    """Write one standing statement, replacing the one it restates, and say which.

    Latest-wins per key rather than append-only, because these are statements about a
    standing intention rather than about a day: an athlete who moves their VO2max target
    from 50 to 52 has one target, and a store keeping both would hand the coach two and
    no way to tell which is current. What was displaced comes back so the caller can read
    it aloud -- which is where a restatement made under a slightly different name gets
    caught, by the athlete, rather than by a synonym table guessing on their behalf.

    Order is by key so that reading the set back is stable across restatements; the file
    is small and read whole, so nothing here depends on the order statements arrived in.
    """
    record = {**content, "recorded_at": _recorded_at(now), "source": ATHLETE_REPORTED_SOURCE}
    root = resolve_state_root(state_dir)
    # 0o700 when this module creates it, matching init_store; an already-existing
    # directory keeps whatever the store gave it.
    root.mkdir(parents=True, mode=0o700, exist_ok=True)
    with _exclusive_lock(root, operation=operation):
        _refuse_when_handed_off(root, operation)
        evidence = load_evidence(root)
        records = evidence[container]
        position = _standing_position(records, key_field, statement_key(content[key_field]))
        replaced = records.pop(position) if position is not None else None
        records.append(record)
        records.sort(key=lambda item: statement_key(str(item.get(key_field) or "")))
        _atomic_json(evidence_path(root), evidence)
        return record, replaced, list(records)


def _retract_standing(
    state_dir: Path | str,
    *,
    container: str,
    key_field: str,
    name: str,
    operation: str,
    label: str,
) -> dict[str, Any]:
    """Remove one standing statement -- the athlete dropping it, not restating it.

    A miss is not an error, for the reason every retraction here says so: the athlete may
    be dropping something they never stored, or naming it differently than they stored it
    under. ``on_record`` names what they do have, so the caller can retry with the stored
    name instead of asking them to guess it back.
    """
    root = resolve_state_root(state_dir)
    root.mkdir(parents=True, mode=0o700, exist_ok=True)
    with _exclusive_lock(root, operation=operation):
        _refuse_when_handed_off(root, operation)
        evidence = load_evidence(root)
        records = evidence[container]
        position = _standing_position(records, key_field, statement_key(name))
        if position is None:
            on_record = _standing_names(records, key_field)
            note = f"no {label} for {name!r} was found to retract"
            if on_record:
                note += f"; on record: {', '.join(on_record)}"
            return {
                "athlete_evidence_version": ATHLETE_EVIDENCE_VERSION,
                "retracted": True,
                "removed": None,
                "on_record": on_record,
                "note": note,
                container: list(records),
            }
        removed = records.pop(position)
        _atomic_json(evidence_path(root), evidence)
        return {
            "athlete_evidence_version": ATHLETE_EVIDENCE_VERSION,
            "retracted": True,
            "removed": removed,
            "on_record": None,
            "note": None,
            container: list(records),
        }


def record_long_term_goal(
    state_dir: Path | str,
    *,
    metric: Any,
    target: Any,
    target_date: Any = None,
    note: Any = None,
    now: dt.datetime | None = None,
) -> dict[str, Any]:
    """Store one thing the athlete is training for beyond the current cycle.

    Two required fields, because they are the only two nothing else can supply: what is
    being aimed at, and where. ``target`` is stored as the athlete said it -- "50",
    "80 kg", "sub-25:00" -- and is never converted, ranged, or checked against anything.
    Whether 50 is close is a reading of evidence this file does not hold, and a store
    that scored it would have coached (AGENTS.md 4).

    There is no field for the current value, deliberately. The provider holds the
    measurements, the athlete's own reported measurements hold the rest, and a third copy
    living beside a target is exactly the parallel durable truth issue #163 exists to
    stop.

    ``target_date`` is optional and taken as given, including a date already past: a goal
    that came due and was not met is a real state, and one the coach should raise rather
    than one the store should refuse. Restating a goal replaces the one on record for the
    same metric, and ``replaced`` says what it displaced.
    """
    content: dict[str, Any] = {
        "metric": _standing_text(metric, "metric"),
        "target": _standing_text(target, "target"),
        "target_date": None,
        "note": None,
    }
    if target_date is not None:
        if not isinstance(target_date, str):
            raise AthleteEvidenceError("target_date must be an ISO date or null")
        try:
            content["target_date"] = dt.date.fromisoformat(target_date).isoformat()
        except ValueError as exc:
            raise AthleteEvidenceError(
                f"target_date must be an ISO date: {target_date!r}"
            ) from exc
    if note is not None:
        content["note"] = _standing_text(note, "note")

    record, replaced, records = _upsert_standing(
        state_dir,
        container="long_term_goals",
        key_field="metric",
        content=content,
        operation="record-long-term-goal",
        now=now,
    )
    return {
        "athlete_evidence_version": ATHLETE_EVIDENCE_VERSION,
        "goal": record,
        "replaced": replaced,
        "long_term_goals": records,
    }


def retract_long_term_goal(state_dir: Path | str, *, metric: Any) -> dict[str, Any]:
    """Drop one long-term goal -- reached, abandoned, or never meant.

    Removal rather than an outcome: whether a goal was hit is a coaching reading of
    evidence, and a store that recorded "achieved" beside it would be making that reading
    from a field the athlete filled in.
    """
    return _retract_standing(
        state_dir,
        container="long_term_goals",
        key_field="metric",
        name=_standing_text(metric, "metric"),
        operation="retracting a long-term goal",
        label="long-term goal",
    )


def record_training_preference(
    state_dir: Path | str,
    *,
    topic: Any,
    statement: Any,
    now: dt.datetime | None = None,
) -> dict[str, Any]:
    """Store one habit the athlete states, in their own words.

    ``topic`` is what the habit is about -- the identity a later restatement lands on --
    and ``statement`` is the habit. Both are free text and neither is parsed. A stored
    preference changes nothing deterministically: no plan is refused for departing from
    one, no session is moved to satisfy one, and nothing here compares a preference
    against what was actually trained. It is context the coach starts from and may argue
    with, out loud, with a reason.

    Only the athlete writes here. A habit read out of activity history is an inference,
    and an inference stored in this file would come back indistinguishable from something
    they said -- so it stays in the context as history, where the coach can weigh it, and
    reaches this file only when the athlete states it themselves.
    """
    content = {
        "topic": _standing_text(topic, "topic"),
        "statement": _standing_text(statement, "statement"),
    }
    record, replaced, records = _upsert_standing(
        state_dir,
        container="training_preferences",
        key_field="topic",
        content=content,
        operation="record-training-preference",
        now=now,
    )
    return {
        "athlete_evidence_version": ATHLETE_EVIDENCE_VERSION,
        "preference": record,
        "replaced": replaced,
        "training_preferences": records,
    }


def retract_training_preference(state_dir: Path | str, *, topic: Any) -> dict[str, Any]:
    """Drop one stated habit, because the athlete no longer holds it.

    The only path by which a preference stops standing. Nothing infers that a habit
    lapsed from the athlete having stopped doing it: three weeks away from a stated five
    strength sessions is a divergence for the coach to raise, and a store that resolved
    it by deleting the five would have answered a question only the athlete can.
    """
    return _retract_standing(
        state_dir,
        container="training_preferences",
        key_field="topic",
        name=_standing_text(topic, "topic"),
        operation="retracting a training preference",
        label="training preference",
    )


def _standing_group(records: list[Any], key_field: str, rows_key: str) -> dict[str, Any] | None:
    """One standing-statement group for the context, or ``None`` when none was stated.

    ``None`` rather than an empty group, because an athlete who has stated nothing is the
    ordinary starting state and the one the coach may ask -- while an empty list beside a
    ``source`` reads like an answer that came back blank. Records too damaged to carry
    their own key are skipped rather than allowed to fail the build, the same stance
    every reader here takes.
    """
    rows = [
        {name: value for name, value in record.items() if name != "source"}
        for record in records
        if isinstance(record, dict)
        and isinstance(record.get(key_field), str)
        and record[key_field].strip()
    ]
    if not rows:
        return None
    return {"source": ATHLETE_REPORTED_SOURCE, rows_key: rows}


def stated_long_term_goals(evidence: dict[str, Any]) -> dict[str, Any] | None:
    """What this athlete is training for past this cycle, or ``None`` if unstated."""
    return _standing_group(evidence.get("long_term_goals") or [], "metric", "goals")


def stated_training_preferences(evidence: dict[str, Any]) -> dict[str, Any] | None:
    """How this athlete says they like to train, or ``None`` if they have not said."""
    return _standing_group(evidence.get("training_preferences") or [], "topic", "preferences")


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


def _activity_positions(
    activities: list[dict[str, Any]], day: str, sport: str
) -> list[int]:
    """Every record held for one sport on one day, in the order they were stored.

    There was only ever one until a file could be imported. A conversation cannot tell a
    correction from a second session -- "40 分鐘，啊是 45" -- so a spoken summary still
    keeps exactly one slot per sport per day. An export can: it carries start times, so
    two runs on one evening arrive as two rows that are provably not one row restated.
    """
    return [
        index
        for index, item in enumerate(activities)
        if item.get("date") == day and item.get("sport") == sport
    ]


def _activity_summary_position(
    activities: list[dict[str, Any]], day: str, sport: str
) -> int | None:
    """The slot a *spoken* summary for one day and sport corrects, or ``None``.

    The athlete's own slot comes first: a restatement corrects what they said before,
    never a row a file supplied. Only when they have said nothing that day does a spoken
    summary land on an imported session, and then only on one it agrees with -- which is
    the caller's check, not this one's.
    """
    positions = _activity_positions(activities, day, sport)
    spoken = [index for index in positions if not activities[index].get("import")]
    if spoken:
        return spoken[0]
    return positions[0] if len(positions) == 1 else None


def same_reported_session(
    first: dict[str, Any], second: dict[str, Any]
) -> bool:
    """Whether two records describe one session -- the whole of this module's dedup.

    Day and sport must match exactly; duration within ``SAME_SESSION_MINUTES``; and, when
    both state a distance, that within ``SAME_SESSION_DISTANCE_KM``. Nothing else is
    consulted, and in particular a start time is not: an export writes local time, a
    second export of the same session writes it in whatever zone that tool used, and two
    timestamps disagreeing by an hour is the single most common way one session looks
    like two.

    Deliberately not a score, a confidence, or a threshold anybody tunes. It answers the
    one question code can answer -- are these the same numbers -- and everything it cannot
    answer is asked of the athlete instead of guessed at (AGENTS.md 4).
    """
    if first.get("date") != second.get("date") or first.get("sport") != second.get("sport"):
        return False
    left, right = first.get("duration_minutes"), second.get("duration_minutes")
    if not isinstance(left, int) or not isinstance(right, int):
        return False
    if abs(left - right) > SAME_SESSION_MINUTES:
        return False
    first_km, second_km = first.get("distance_km"), second.get("distance_km")
    if isinstance(first_km, (int, float)) and isinstance(second_km, (int, float)):
        if abs(float(first_km) - float(second_km)) > SAME_SESSION_DISTANCE_KM:
            return False
    return True


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
        series.append(
            {
                "date": day.isoformat(),
                **values,
                # The record's own provenance, for the same reason the reported sessions
                # carry theirs: a number the athlete read off a scale this morning and one
                # an export supplied are both their word, and a coach reading a series
                # should be able to see which days are which.
                "source": (
                    record.get("source")
                    if record.get("source") in (ATHLETE_REPORTED_SOURCE, ATHLETE_IMPORTED_SOURCE)
                    else ATHLETE_REPORTED_SOURCE
                ),
            }
        )
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
        if position is None:
            # No slot of the athlete's own that day. An upload may still hold this exact
            # session, and landing beside it would put one session in the context twice --
            # the same double count the import path refuses from its side. So a spoken
            # summary takes over a session a file already holds when the two agree, and
            # only when exactly one of them does.
            same = [
                index
                for index in _activity_positions(activities, content["date"], parsed_sport)
                if same_reported_session(activities[index], content)
            ]
            position = same[0] if len(same) == 1 else None
        if position is not None and activities[position].get("summary_id") == summary_id:
            return {
                "summary_id": summary_id,
                "idempotent_replay": True,
                "replaced": None,
                "activity": activities[position],
                "activity_count": len(activities),
            }
        standing = activities[position] if position is not None else None
        summary = {
            "summary_id": summary_id,
            **content,
            "recorded_at": recorded_at,
            "source": ATHLETE_REPORTED_SOURCE,
            # What told this session apart from another of the same sport that day, when
            # anything did. A spoken summary never carries one -- the athlete said a day,
            # not a clock time -- so it is null here and only an import fills it.
            "started_at": standing.get("started_at") if standing else None,
            # Where the record came from, kept even when the athlete has just restated its
            # numbers: this is now their word about a session an upload also described, and
            # the two facts are both true. `dedup_keys` travels with it so re-uploading the
            # same export still recognises this session instead of adding it again.
            "import": standing.get("import") if standing else None,
            "dedup_keys": list(standing.get("dedup_keys") or []) if standing else [],
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
            # the one place the spoken-summary limitation can actually bite: a caller that
            # meant to add a second session has just overwritten the first, and it can
            # see so here rather than after the athlete notices a missing run.
            "replaced_note": _replaced_activity_note(replaced, parsed_sport, content["date"]),
        }


def _replaced_activity_note(
    replaced: dict[str, Any] | None, sport: str, day: str
) -> str | None:
    """What a restatement displaced, in the terms of what it displaced.

    Two different things can stand in that slot, and telling the athlete the wrong one is
    worse than telling them nothing: overwriting their own earlier sentence is the version
    limitation ("say both sessions as one"), while overwriting a session an upload
    supplied is not a limitation at all -- it is the correction working, and the upload's
    own reference stays on the record so re-importing that file still recognises it.
    """
    if replaced is None:
        return None
    if replaced.get("import"):
        return (
            f"this restates a {sport} session for {day} that an upload had already "
            "recorded; the upload's own reference is kept, so re-importing that file "
            "will not add the session a second time"
        )
    return (
        f"a {sport} summary for {day} was already on record and has been replaced; one "
        "spoken summary per sport per day is held, so two genuinely distinct sessions "
        "belong in one combined summary -- or in an upload, which carries start times "
        "and can hold both"
    )


def retract_activity_summary(
    state_dir: Path | str,
    *,
    sport: Any,
    date: Any = None,
    started_at: Any = None,
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

    An upload can leave two sessions of one sport on one day -- two runs with two start
    times, which a conversation could never have distinguished. Then ``sport`` and ``date``
    no longer name a record, and this refuses rather than removing the first of them:
    ``candidates`` lists what it found, each with its start time and duration, and
    ``started_at`` picks one. Deleting the wrong session silently is the one outcome worth
    a second turn of conversation to avoid.
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
    if started_at is not None and (not isinstance(started_at, str) or not started_at.strip()):
        raise AthleteEvidenceError("started_at must be a non-empty string or null")
    # Matched as the stored string rather than as a moment. It is the value this product
    # handed back in `candidates`, so an athlete choosing one of them is choosing a label,
    # and re-parsing it into a time would only add a way for the choice to miss.
    selected_start = started_at.strip() if isinstance(started_at, str) else None

    root = resolve_state_root(state_dir)
    # 0o700 when this module creates it, matching init_store; an already-existing
    # directory keeps whatever the store gave it.
    root.mkdir(parents=True, mode=0o700, exist_ok=True)
    with _exclusive_lock(root, operation="retracting a reported activity"):
        _refuse_when_handed_off(root, "retracting a reported activity")
        evidence = load_evidence(root)
        activities = evidence["reported_activities"]
        positions = _activity_positions(activities, day, parsed_sport)
        if selected_start is not None:
            positions = [
                index
                for index in positions
                if str(activities[index].get("started_at") or "") == selected_start
            ]
        if not positions:
            on_record = _names_on_record(activities, day, "sport")
            miss_note = f"no {parsed_sport} summary for {day} was found to retract"
            if selected_start is not None:
                miss_note += f" starting {selected_start}"
            if on_record:
                miss_note += f"; on record for that day: {', '.join(on_record)}"
            return {
                "retracted": True,
                "removed": None,
                "activity_count": len(activities),
                "on_record_that_day": on_record,
                "candidates": [],
                "note": miss_note,
            }
        if len(positions) > 1:
            return {
                "retracted": False,
                "removed": None,
                "activity_count": len(activities),
                "on_record_that_day": None,
                "candidates": [_activity_candidate(activities[index]) for index in positions],
                "note": (
                    f"{len(positions)} {parsed_sport} sessions are on record for {day}, so "
                    "this names more than one; say which by passing its started_at, and "
                    "nothing has been removed"
                ),
            }
        removed = activities.pop(positions[0])
        _atomic_json(evidence_path(root), evidence)
        return {
            "retracted": True,
            "removed": removed,
            "activity_count": len(activities),
            "on_record_that_day": None,
            "candidates": [],
            "note": None,
        }


def _activity_candidate(record: dict[str, Any]) -> dict[str, Any]:
    """One session offered back so the athlete can say which of several they meant."""
    return {
        "started_at": record.get("started_at"),
        "duration_minutes": record.get("duration_minutes"),
        "distance_km": record.get("distance_km"),
        "source": record.get("source"),
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
        provenance = record.get("import") if isinstance(record.get("import"), dict) else None
        summaries.append(
            {
                **{name: record.get(name) for name in _ACTIVITY_SUMMARY_FIELDS},
                "date": day.isoformat(),
                # The record's own provenance, not a constant. A session the athlete
                # described and one a file supplied are both their evidence and neither is
                # a provider actual, but they are different claims: a coach reading a
                # progression needs to know whether the numbers came from a device's
                # export or from somebody remembering the session.
                "source": (
                    record.get("source")
                    if record.get("source") in (ATHLETE_REPORTED_SOURCE, ATHLETE_IMPORTED_SOURCE)
                    else ATHLETE_REPORTED_SOURCE
                ),
                # Which upload it came from, in the athlete's own words for it when they
                # gave one ("Garmin Connect 2019-2026") and the format otherwise. Null for
                # a session that was spoken, which is the majority.
                "imported_from": (
                    (provenance.get("source_name") or provenance.get("recognised_as")
                     or provenance.get("format"))
                    if provenance
                    else None
                ),
            }
        )
    summaries.sort(key=lambda item: (item["date"], item["sport"]))
    summaries.sort(key=lambda item: item["date"], reverse=True)
    return summaries


# --------------------------------------------------------------------------------------
# How the athlete says they feel (issue #188)
# --------------------------------------------------------------------------------------


def _state_note(value: Any) -> str:
    """One subjective statement, in the athlete's words, inside the length this carries."""
    if not isinstance(value, str) or not value.strip():
        raise AthleteEvidenceError("note must be a non-empty string")
    note = value.strip()
    if len(note) > SUBJECTIVE_STATE_MAX_CHARS:
        raise AthleteEvidenceError(
            f"note must be at most {SUBJECTIVE_STATE_MAX_CHARS} characters, found {len(note)}"
        )
    return note


def record_subjective_state(
    state_dir: Path | str,
    *,
    note: Any,
    date: Any = None,
    timezone_name: str = DEFAULT_TIMEZONE,
    now: dt.datetime | None = None,
) -> dict[str, Any]:
    """Store how the athlete says they felt on one day, in their own sentence.

    Two fields and no third: the words and the day. There is no scale, no category and no
    severity, because every one of those would be a reading of the sentence made here
    rather than by the coach -- and a reading made here is the one that arrives looking
    like a fact. ``recovery_signals`` refuses a subjective feeling translated into a
    number, correctly and still; this stores the sentence instead, which is the thing that
    ban was protecting.

    Nothing is triggered by writing one. No plan is refused, no session is moved, no rest
    day is implied, and no validator reads this container at all. It reaches the coach as
    the last fortnight of what the athlete said, dated, and what a run of them means is the
    coach's judgment over them (AGENTS.md 4, 5).

    **This is not the symptom channel.** Pain, illness, chest pain, dizziness and unusual
    symptoms belong in the session request's ``red_flags``, where an explicit true limits
    the day deterministically (AGENTS.md 9). Nothing here inspects the sentence to find out
    which of the two it is -- a store that read the words would be diagnosing from prose,
    and would fail exactly on the phrasings that matter. The boundary is stated in the tool
    descriptions on both sides of it, where the model choosing between them reads.

    ``date`` defaults to today in the athlete's own timezone and may not be in their
    future, the same rule every dated statement here follows: "昨天很累" is an ordinary
    thing to say a day late, and "tomorrow was hard" is not a report.

    One note per day, and the newest wins. The response names what a restatement displaced
    so nothing is lost quietly.
    """
    stated = _state_note(note)
    parsed_date = _reported_date(date, today=athlete_today(timezone_name, now))
    recorded_at = _recorded_at(now)

    root = resolve_state_root(state_dir)
    # 0o700 when this module creates it, matching init_store; an already-existing
    # directory keeps whatever the store gave it.
    root.mkdir(parents=True, mode=0o700, exist_ok=True)
    with _exclusive_lock(root, operation="recording a subjective state"):
        _refuse_when_handed_off(root, "recording a subjective state")
        evidence = load_evidence(root)
        states = evidence["subjective_states"]
        day = parsed_date.isoformat()
        position = _measurement_position(states, day)
        held = states[position] if position is not None else {}
        content = {"date": day, "note": stated}
        state_id = canonical_hash(content)
        if position is not None and held.get("state_id") == state_id:
            return {
                "athlete_evidence_version": ATHLETE_EVIDENCE_VERSION,
                "state_id": state_id,
                "idempotent_replay": True,
                "replaced": None,
                "state": held,
                "state_count": len(states),
            }
        state = {
            "state_id": state_id,
            **content,
            "recorded_at": recorded_at,
            "source": ATHLETE_REPORTED_SOURCE,
        }
        replaced: dict[str, Any] | None = None
        if position is None:
            states.append(state)
        else:
            replaced = held
            states[position] = state
        _atomic_json(evidence_path(root), evidence)
        return {
            "athlete_evidence_version": ATHLETE_EVIDENCE_VERSION,
            "state_id": state_id,
            "idempotent_replay": False,
            "replaced": replaced,
            "state": state,
            "state_count": len(states),
        }


def retract_subjective_state(
    state_dir: Path | str,
    *,
    date: Any = None,
    note: Any = None,
    timezone_name: str = DEFAULT_TIMEZONE,
    now: dt.datetime | None = None,
) -> dict[str, Any]:
    """Remove one day's note -- the athlete taking it back rather than correcting it.

    Keyed by ``date`` alone, which is the whole of a note's identity here for the reason a
    body measurement's is: one per day, so there is no second name to get wrong. ``note``
    is accepted only so a retraction that also tries to carry one is refused by name -- a
    statement that says the day's words should not stand cannot also state them.

    A retraction that finds nothing is not an error, which is what keeps it safe to repeat.
    """
    if note is not None:
        raise AthleteEvidenceError(
            "a retraction states the day's note should not stand; a new note belongs in "
            "a fresh subjective state instead"
        )
    day = _reported_date(date, today=athlete_today(timezone_name, now)).isoformat()

    root = resolve_state_root(state_dir)
    # 0o700 when this module creates it, matching init_store; an already-existing
    # directory keeps whatever the store gave it.
    root.mkdir(parents=True, mode=0o700, exist_ok=True)
    with _exclusive_lock(root, operation="retracting a subjective state"):
        _refuse_when_handed_off(root, "retracting a subjective state")
        evidence = load_evidence(root)
        states = evidence["subjective_states"]
        position = _measurement_position(states, day)
        if position is None:
            return {
                "retracted": True,
                "removed": None,
                "state_count": len(states),
                "note": f"no subjective state for {day} was found to retract",
            }
        removed = states.pop(position)
        _atomic_json(evidence_path(root), evidence)
        return {
            "retracted": True,
            "removed": removed,
            "state_count": len(states),
            "note": None,
        }


def reported_subjective_states(
    evidence: dict[str, Any], start: dt.date, end: dt.date
) -> list[dict[str, Any]]:
    """What the athlete said about how they felt, between ``start`` and ``end``.

    Newest day first, each row the sentence and its date and nothing else. Nothing is
    counted, grouped, scored or compared against a recovery reading -- "three weeks of
    this" is a pattern the coach sees in the rows, and a run length computed here would be
    that judgment made in the wrong layer (AGENTS.md 4, 5).

    A record too damaged to place on a date, or carrying no readable sentence, is skipped
    rather than allowed to fail the whole build -- the stance every reader in this file
    takes.
    """
    rows: list[dict[str, Any]] = []
    for record in evidence.get("subjective_states") or []:
        if not isinstance(record, dict):
            continue
        try:
            day = dt.date.fromisoformat(str(record.get("date")))
        except ValueError:
            continue
        if not (start <= day <= end):
            continue
        stated = record.get("note")
        if not isinstance(stated, str) or not stated.strip():
            continue
        rows.append(
            {
                "date": day.isoformat(),
                "note": stated,
                "recorded_at": record.get("recorded_at"),
            }
        )
    rows.sort(key=lambda item: item["date"], reverse=True)
    return rows


# --------------------------------------------------------------------------------------
# Importing a file the athlete uploaded (issue #140 phase 2)
# --------------------------------------------------------------------------------------

# What a caller may say about a session this product already holds, when code could not
# tell whether the two are one. Both answers are the athlete's to give; neither is
# guessed at, and an unanswered question leaves the row unwritten rather than assumed.
IMPORT_RESOLUTIONS = ("same_session", "separate_session")


def _dedup_key(row: dict[str, Any], source: str) -> str:
    """What makes one imported session recognisable the next time the same file arrives.

    An id the source assigned is the identity when there is one -- namespaced by the
    export it came from, because a Strava activity 12345 and an Intervals activity 12345
    are two sessions with one number. Without an id, the session's own numbers are its
    identity: the same export re-uploaded reads the same rows, so the same key falls out.
    """
    external = row.get("external_id")
    if isinstance(external, str) and external.strip():
        return canonical_hash({"source": source, "external_id": external.strip()})
    return canonical_hash(
        {
            "date": row.get("date"),
            "sport": row.get("sport"),
            "duration_minutes": row.get("duration_minutes"),
            "distance_km": row.get("distance_km"),
            "started_at": row.get("started_at"),
        }
    )


def _validated_resolutions(value: Any) -> dict[str, str]:
    if value is None:
        return {}
    if not isinstance(value, list):
        raise AthleteEvidenceError("resolutions must be an array of answers")
    answers: dict[str, str] = {}
    for item in value:
        if not isinstance(item, dict):
            raise AthleteEvidenceError("each resolution must be an object")
        conflict_id = item.get("conflict_id")
        answer = item.get("resolution")
        if not isinstance(conflict_id, str) or not conflict_id.strip():
            raise AthleteEvidenceError("each resolution needs the conflict_id it answers")
        if answer not in IMPORT_RESOLUTIONS:
            raise AthleteEvidenceError(
                f"resolution must be one of {', '.join(IMPORT_RESOLUTIONS)}, found {answer!r}"
            )
        answers[conflict_id.strip()] = answer
    return answers


def _closest_duration(activities: list[dict[str, Any]], positions: list[int], row: dict[str, Any]):
    """Which standing record an ambiguous row is closest to, by duration alone.

    Only reached once the athlete has already said the two are one session. It picks
    *which* one they meant when several stand on that day; it never decides that they are
    the same, which is the part code has no business answering.
    """
    minutes = row.get("duration_minutes")
    return min(
        positions,
        key=lambda index: abs(int(activities[index].get("duration_minutes") or 0) - int(minutes)),
    )


def import_reported_evidence(
    state_dir: Path | str,
    *,
    activities: list[dict[str, Any]],
    measurements: list[dict[str, Any]] | None = None,
    unreadable: list[dict[str, Any]] | None = None,
    format_name: str,
    recognised_as: str | None = None,
    digest: str,
    source_name: Any = None,
    resolutions: Any = None,
    now: dt.datetime | None = None,
) -> dict[str, Any]:
    """Write one already-parsed upload into the evidence the athlete states by talking.

    Takes rows, never a file: the reading is ``evidence_import``'s job and the writing is
    this module's, and keeping them apart is what lets a new format be a new reader rather
    than a new store.

    **Four outcomes per session, and only one of them asks anything.**

    - *skipped* -- this product already holds it under the same identity. Re-uploading a
      file is the ordinary case, not an error, and it changes nothing.
    - *merged* -- a record already stands for that day and sport and agrees on the
      numbers, so it is one session described twice. The standing record is left exactly
      as it is and the upload's key is written onto it, which is what makes the *next*
      re-upload a skip.
    - *added* -- nothing on record resembles it.
    - *needs_confirmation* -- something stands on that day and sport and does *not* agree.
      That is either a correction or a second session, and nothing here can tell which, so
      nothing is written and the question is handed back with both readings. Answering it
      is re-sending the same payload with ``resolutions``; the payload is parsed the same
      way twice, so the ``conflict_id`` an answer names is the row it was asked about.

    Re-sending a finished upload is recognised by its digest and writes nothing at all. An
    upload that ended with questions is deliberately *not* recorded as finished, so the
    answering call still has work to do.
    """
    answers = _validated_resolutions(resolutions)
    if source_name is not None and (not isinstance(source_name, str) or not source_name.strip()):
        raise AthleteEvidenceError("source_name must be a non-empty string or null")
    label = source_name.strip() if isinstance(source_name, str) else None
    source = recognised_as or format_name
    import_id = f"import-{digest[:16]}"
    recorded_at = _recorded_at(now)

    root = resolve_state_root(state_dir)
    root.mkdir(parents=True, mode=0o700, exist_ok=True)
    with _exclusive_lock(root, operation="importing reported evidence"):
        _refuse_when_handed_off(root, "importing reported evidence")
        evidence = load_evidence(root)
        finished = next(
            (item for item in evidence["imports"] if item.get("digest") == digest), None
        )
        if finished is not None:
            return {
                "import_id": finished.get("import_id"),
                "already_imported": True,
                "added": [],
                "merged": [],
                "skipped": [],
                "needs_confirmation": [],
                "measurements_added": [],
                "measurements_skipped": [],
                "unreadable": list(unreadable or []),
                "counts": finished.get("counts"),
                "note": (
                    "this upload was imported before and nothing was written again; its "
                    "sessions are already on record"
                ),
            }

        stored = evidence["reported_activities"]
        held_keys = {
            key
            for record in stored
            for key in (record.get("dedup_keys") or [])
            if isinstance(key, str)
        }
        added: list[dict[str, Any]] = []
        merged: list[dict[str, Any]] = []
        skipped: list[dict[str, Any]] = []
        conflicts: list[dict[str, Any]] = []
        # Records this upload has already accounted for -- ones it wrote, and ones it
        # recognised as sessions it also describes. Neither is something a *later* row of
        # the same upload has to be disambiguated against: a file listing a 45-minute run
        # and a 30-minute run on one Monday has *said* they are two sessions, with two
        # start times, and asking the athlete which of them is real would be asking them
        # to re-read the file they just handed over.
        #
        # Merged targets belong here as much as written ones, which is the correction
        # this fixes. An athlete who had said "跑了 45 分鐘" and then uploads the file
        # holding that run plus an evening one used to be asked about the evening run:
        # the morning row merged into their spoken record, and the evening row then found
        # that record still standing and unaccounted for. It was accounted for -- by the
        # row immediately before it, out of the same file.
        #
        # A duplicate *inside* one export still merges; that is the sameness rule, not a
        # question, and it runs before any of this.
        written_here: set[int] = set()

        provenance = {
            "import_id": import_id,
            "format": format_name,
            "recognised_as": recognised_as,
            "source_name": label,
        }

        for row in activities:
            key = _dedup_key(row, source)
            if key in held_keys:
                skipped.append({**_imported_summary(row), "reason": "already on record"})
                continue
            positions = _activity_positions(stored, row["date"], row["sport"])
            same = [index for index in positions if same_reported_session(stored[index], row)]
            standing = [index for index in positions if index not in written_here]
            conflict_id = canonical_hash({"digest": digest, "key": key})[:16]
            answer = answers.get(conflict_id)
            if same:
                target = same[0]
            elif standing and answer == "same_session":
                target = _closest_duration(stored, standing, row)
            elif standing and answer != "separate_session":
                conflicts.append(_import_conflict(conflict_id, row, stored, standing))
                continue
            else:
                target = None
            if target is not None:
                keys = list(stored[target].get("dedup_keys") or [])
                keys.append(key)
                stored[target]["dedup_keys"] = keys
                held_keys.add(key)
                written_here.add(target)
                merged.append(
                    {
                        **_imported_summary(row),
                        "merged_into": _activity_candidate(stored[target]),
                    }
                )
                continue
            record = {
                "summary_id": canonical_hash(
                    {name: row.get(name) for name in _ACTIVITY_SUMMARY_FIELDS}
                ),
                **{name: row.get(name) for name in _ACTIVITY_SUMMARY_FIELDS},
                "recorded_at": recorded_at,
                "source": ATHLETE_IMPORTED_SOURCE,
                "started_at": row.get("started_at"),
                "import": {**provenance, "external_id": row.get("external_id")},
                "dedup_keys": [key],
            }
            written_here.add(len(stored))
            stored.append(record)
            held_keys.add(key)
            added.append(_imported_summary(row))

        measurements_added, measurements_skipped = _import_measurements(
            evidence, measurements or [], provenance=provenance, recorded_at=recorded_at
        )

        counts = {
            "sessions_read": len(activities),
            "added": len(added),
            "merged": len(merged),
            "skipped": len(skipped),
            "needs_confirmation": len(conflicts),
            "unreadable": len(unreadable or []),
            "measurements_added": len(measurements_added),
            "measurements_skipped": len(measurements_skipped),
        }
        if not conflicts:
            # Recorded as finished only when nothing is left to answer. An upload still
            # holding questions must stay unrecorded, or the call carrying the answers
            # would be recognised as a repeat of itself and write nothing.
            evidence["imports"].append(
                {
                    "import_id": import_id,
                    "digest": digest,
                    "format": format_name,
                    "recognised_as": recognised_as,
                    "source_name": label,
                    "imported_at": recorded_at,
                    "counts": counts,
                }
            )
        _atomic_json(evidence_path(root), evidence)
        return {
            "import_id": import_id,
            "already_imported": False,
            "added": added,
            "merged": merged,
            "skipped": skipped,
            "needs_confirmation": conflicts,
            "measurements_added": measurements_added,
            "measurements_skipped": measurements_skipped,
            "unreadable": list(unreadable or []),
            "counts": counts,
            "note": _import_note(counts),
        }


def _imported_summary(row: dict[str, Any]) -> dict[str, Any]:
    """One session as the report names it -- what was read, not how it was stored."""
    return {
        "date": row.get("date"),
        "sport": row.get("sport"),
        "duration_minutes": row.get("duration_minutes"),
        "distance_km": row.get("distance_km"),
        "started_at": row.get("started_at"),
        "dropped": list(row.get("dropped") or []),
    }


def _import_conflict(
    conflict_id: str,
    row: dict[str, Any],
    stored: list[dict[str, Any]],
    positions: list[int],
) -> dict[str, Any]:
    """One question, with both readings stated rather than one of them chosen."""
    return {
        "conflict_id": conflict_id,
        "incoming": _imported_summary(row),
        "on_record": [_activity_candidate(stored[index]) for index in positions],
        "question": (
            f"a {row['sport']} session is already on record for {row['date']} with a "
            "different duration: is the uploaded one the same session stated differently, "
            "or a second session that day?"
        ),
    }


def _import_measurements(
    evidence: dict[str, Any],
    measurements: list[dict[str, Any]],
    *,
    provenance: dict[str, Any],
    recorded_at: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Weights and body fat out of an upload, one record per day, existing records untouched.

    An export holds every weigh-in, sometimes several a day; the store holds one record a
    day, so the last figure the file states for a day is the one kept -- an export is
    written in order, and the last reading of a morning is the one an athlete would quote.

    A day this athlete has already stated is skipped rather than overwritten. The reason is
    the module's own rule read from the other end: the newest *statement* wins, and a file
    exported last year is not a newer statement than the number they gave yesterday.
    """
    by_day: dict[str, dict[str, Any]] = {}
    for entry in measurements:
        day = str(entry.get("date"))
        field = entry.get("field")
        if field not in BODY_MEASUREMENT_VALUES:
            continue
        low, high = BODY_MEASUREMENT_BOUNDS[field]
        value = entry.get("value")
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            continue
        if not low <= float(value) <= high:
            # Out of bounds is refused here exactly as it is refused from a conversation:
            # a scale read as 7.23 kg is a typo whichever door it came through.
            continue
        by_day.setdefault(day, {})[field] = round(float(value), 2)

    stored = evidence["body_measurements"]
    added: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for day in sorted(by_day):
        try:
            dt.date.fromisoformat(day)
        except ValueError:
            continue
        values = by_day[day]
        if _measurement_position(stored, day) is not None:
            skipped.append({"date": day, **values, "reason": "already on record"})
            continue
        record = {
            "measurement_id": canonical_hash({"date": day, **values}),
            "date": day,
            "weight_kg": values.get("weight_kg"),
            "body_fat_pct": values.get("body_fat_pct"),
            "recorded_at": recorded_at,
            "source": ATHLETE_IMPORTED_SOURCE,
            "import": provenance,
        }
        stored.append(record)
        added.append({"date": day, **values})
    return added, skipped


def _import_note(counts: dict[str, int]) -> str:
    """One sentence naming what happened, including the parts nobody would ask about."""
    parts = [
        f"{counts['added']} added",
        f"{counts['merged']} already described by a session on record",
        f"{counts['skipped']} already imported",
    ]
    if counts["needs_confirmation"]:
        parts.append(f"{counts['needs_confirmation']} needing an answer before they are written")
    if counts["unreadable"]:
        parts.append(f"{counts['unreadable']} unreadable and named in full")
    if counts["measurements_added"] or counts["measurements_skipped"]:
        parts.append(
            f"{counts['measurements_added']} body measurements added, "
            f"{counts['measurements_skipped']} left as already stated"
        )
    return "; ".join(parts)
