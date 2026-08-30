"""Twenty-one fixed ``startCoachSession`` reads, and the command that re-blesses them.

This module holds the scenarios; ``test_coach_session_scenarios.py`` holds what is
asserted about them. They are separate files because the same definitions are read by
two different acts: a test run, which must never write, and a regeneration, which must
never happen by accident.

Why this exists at all. On 2026-08-24 a change that reduced how often a coaching turn
reads intervals.icu was verified once, by hand, with a throwaway script that ran a set
of scenarios at two git checkouts and diffed the results field by field. It answered the
question and then stopped being runnable: it needed a second checkout, so neither CI nor
the next reader could repeat it, and the evidence it produced lived in a temporary
directory. The question it answered is not a one-off -- *the code still reads everything
it read* is exactly what the next refactor of the same read path would silently give
back. So the "before" is committed here instead of being fetched from an older commit,
and the "after" is whatever this checkout does today.

What a scenario is: one ``CoachGateway.start_session`` call -- the method
``startCoachSession`` is -- against a fresh temporary store, the repository's own
``FakeIntervals`` double, and one injected instant. Nothing here reaches a network, and
nothing here can touch the athlete's real store: every run builds its state under
``tempfile.TemporaryDirectory()`` and throws it away.

Everything in these scenarios is synthetic, and is either the anonymous example plan in
``examples/garmin-coach-loop-28-day`` or written here (AGENTS.md 2).

Determinism, which is what makes a committed snapshot possible at all:

* one fixed ``now`` per scenario, injected into the gateway, so no clock is read;
* a fixed owner id, so the state directory path is the same every run;
* provider answers that are literal data in this file, never a recorded live payload.

Regenerating:

    python3 -m tests.coach_session_scenarios --write

That is the only thing that writes ``tests/fixtures/coach_session_scenarios``. It is a
separate command rather than a flag on the test run on purpose: a snapshot that a test
can refresh on its way past stops being evidence of anything, because the run that
should have failed rewrites the thing it was measured against. Regenerating leaves a
diff, and the diff is the review.
"""

from __future__ import annotations

import argparse
import copy
import datetime as dt
import json
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable
from unittest import mock

from garmin_coach_loop import athlete_evidence
from garmin_coach_loop import evidence_import
from garmin_coach_loop import store as store_module
from garmin_coach_loop.context_core import ContextBuildError
from garmin_coach_loop.gateway import CoachGateway, GatewayConfig
from garmin_coach_loop.prescription import render_prescription

from garmin_coach_loop.source_intervals import ProviderResponse
from tests.test_gateway import FakeIntervals, RUN_SPORT_SETTINGS, publishable_plan


ROOT = Path(__file__).resolve().parents[1]
SNAPSHOTS = Path(__file__).resolve().parent / "fixtures" / "coach_session_scenarios"
EXAMPLE = ROOT / "examples" / "garmin-coach-loop-28-day"

# The committed example turn, reused as the shape a seeded commit is made under. Only a
# scenario that writes more than one plan version needs it -- everything else inits the
# store once and never commits again.
EXAMPLE_CONTEXT = json.loads(
    (EXAMPLE / "coach-context-day-4.json").read_text(encoding="utf-8")
)
EXAMPLE_EVENT = json.loads(
    (EXAMPLE / "decision-event-day-4.json").read_text(encoding="utf-8")
)

# Named once here and quoted in every failure message. A snapshot nobody can regenerate
# is deleted the first time it fails, so the way back has to travel with the failure.
REGENERATE_COMMAND = "python3 -m tests.coach_session_scenarios --write"

# A fixed, canonical-UUID-shaped owner id. It never appears in a response -- the gateway
# never echoes owner identity back -- so its only job is to give ``resolve_state_dir``
# something stable to build a path under, which removes one more reason two runs of the
# same scenario could differ.
OWNER_ID = "00000000-0000-4000-8000-000000000001"
TOKEN = "tok-scenario-fixture"

# Synthetic throughout and short on purpose: nothing here should read as credential
# material to the safety scanner, and nothing here has ever been real.
# The ``_VALUE`` suffixes are not decoration: the repository safety scanner reads
# ``client_secret = "..."`` as an assigned secret whatever the string says, and
# tests/test_gateway.py names its own synthetic pair the same way for the same reason.
HMAC_KEY = b"unit-test-fingerprint-key-0000000"
CLIENT_ID_VALUE = "test-client"
CLIENT_SECRET_VALUE = "test-only-not-real"

# Thursday, day 4 of the example plan's 28-day cycle (2026-08-10 .. 2026-09-06). Today's
# session is ``run-quality-01``, the one session of the week the plan prescribes more
# than one step for -- which is what makes the per-segment read fire. Same instant as
# ``tests/test_gateway.py``'s own ``NOW``, so a scenario here and a unit test there
# describe the same day.
NOW_TODAY = dt.datetime(2026, 8, 13, 0, 0, tzinfo=dt.timezone.utc)
# Monday, day 8. The previous calendar week (Mon..Sun) is exactly the plan's one stored
# week, which is what a week review reads over.
NOW_WEEK_REVIEW = dt.datetime(2026, 8, 17, 0, 30, tzinfo=dt.timezone.utc)
# Day 26 of the same 28-day cycle: late enough that a cycle review has a cycle to review,
# early enough that the cycle has not ended.
NOW_CYCLE_REVIEW = dt.datetime(2026, 9, 4, 0, 30, tzinfo=dt.timezone.utc)
# Monday, day 22: the day after week three ends, with weeks one and two already rolled
# forward at the same weekly rhythm -- the same "day after the reviewed week" shape as
# NOW_WEEK_REVIEW, two cycle-weeks later. This is the boundary a plan_week turn for week
# four begins from.
NOW_PLAN_WEEK_FOUR = dt.datetime(2026, 8, 31, 0, 30, tzinfo=dt.timezone.utc)
# Wednesday evening, day 3: after mobility-01 -- the day's own prescribed session -- has
# happened, and before run-quality-01, Thursday's threshold anchor. The example plan's
# static match_status already reads Monday through Wednesday as completed, so this is
# the latest instant in week one that stays consistent with that -- one day earlier and
# Wednesday's own session would read completed before it happened.
NOW_BEFORE_THE_KEY_SESSION = dt.datetime(2026, 8, 12, 21, 0, tzinfo=dt.timezone.utc)


# -- provider fixtures -------------------------------------------------------------


def activity_row(
    activity_id: str,
    date: str,
    *,
    minutes: float,
    distance_m: float,
    avg_speed: float,
    hr: float | None = None,
    paired_event_id: str | None = None,
    sport: str = "Run",
    indoors: bool = False,
) -> dict[str, Any]:
    """One row in the shape intervals.icu answers ``/activities`` with.

    ``trainer`` is carried on every row, set for an indoor recording and null
    otherwise -- the live shape, verified 2026-08-26 across six weeks of this
    account's runs. Null there is the provider saying "not indoors", which is a
    different answer from a row that omits the key altogether; the omitted case is a
    unit test in ``test_intervals_source.py`` rather than a whole read here.
    """
    row: dict[str, Any] = {
        "id": activity_id,
        "type": sport,
        "start_date_local": f"{date}T07:00:00",
        "moving_time": minutes * 60,
        "distance": distance_m,
        "average_speed": avg_speed,
        "trainer": True if indoors else None,
    }
    if hr is not None:
        row["average_heartrate"] = hr
    if paired_event_id is not None:
        row["paired_event_id"] = paired_event_id
    return row


def segment_row(
    *,
    seconds: float,
    meters: float,
    hr: float,
    max_hr: float | None = None,
    min_hr: float | None = None,
) -> dict[str, Any]:
    """One row in the shape intervals.icu answers ``/activity/{id}/intervals`` with.

    ``type`` is always ``WORK`` because that is what the provider actually returns.
    Verified live 2026-08-20 on a prescribed warm-up plus four reps plus a cool-down:
    thirteen segments came back, every one of them typed ``WORK``, every ``label``
    null. A fixture that helpfully typed the recoveries ``RECOVERY`` would hand the
    coach a reading the provider does not give it, and any measurement taken against
    that fixture would be measuring the fixture.
    """
    return {
        "type": "WORK",
        "distance": meters,
        "moving_time": seconds,
        "average_speed": meters / seconds,
        "average_heartrate": hr,
        "max_heartrate": max_hr if max_hr is not None else hr + 6,
        "min_heartrate": min_hr if min_hr is not None else hr - 10,
    }


def wellness_rows(end_date: str, *, days: int = 7) -> list[dict[str, Any]]:
    """A full wellness feed ending on ``end_date``, every field this product reads."""
    end = dt.date.fromisoformat(end_date)
    return [
        {
            "id": (end - dt.timedelta(days=days - 1 - offset)).isoformat(),
            "sleepSecs": 25200,
            "sleepScore": 70,
            "hrv": 65,
            "restingHR": 48,
        }
        for offset in range(days)
    ]


def sparse_wellness_rows(end_date: str, *, days: int = 7) -> list[dict[str, Any]]:
    """Rows that exist and say almost nothing -- the shape AGENTS.md 3 is about.

    A day the provider answered for but carried no value on is not a day of good
    recovery and not a day of bad recovery; it is a day with no reading. The last day
    carries a resting heart rate alone so the difference between "no rows" and "rows
    with nothing in them" stays visible in the snapshot.
    """
    end = dt.date.fromisoformat(end_date)
    rows: list[dict[str, Any]] = [
        {"id": (end - dt.timedelta(days=days - 1 - offset)).isoformat()}
        for offset in range(days)
    ]
    rows[-1]["restingHR"] = 55
    return rows


def recovery_upload(end_date: str, *, days: int = 2) -> dict[str, Any]:
    """A client-uploaded ``recovery_signals`` payload for the window ending ``end_date``.

    The gateway refuses any row outside the session's own build window, so this cannot
    be one shared constant across scenarios that run at different instants -- it is
    built from the scenario's own ``now``. This is also the only source of
    ``readiness_score``, ``hrv_status`` and the body-battery figures: the provider's
    ``/wellness`` feed carries none of them, so a scenario that wants them in its
    context has to hand them in.
    """
    end = dt.date.fromisoformat(end_date)
    return {
        "source": "client-upload:recovery",
        "days": [
            {
                "date": (end - dt.timedelta(days=days - 1 - offset)).isoformat(),
                "readiness_score": 55.0 + offset,
                "readiness_level": "MODERATE",
                "hrv_status": "BALANCED",
                "hrv_7d_avg_ms": 70.0,
                "acute_load": 400.0 + offset * 10,
                "recovery_time_sec": 3600.0,
                "body_battery_high": 80.0,
                "body_battery_low": 30.0,
                "avg_stress": 25.0,
            }
            for offset in range(days)
        ],
    }


def declining_recovery_upload(end_date: str, *, days: int = 3) -> dict[str, Any]:
    """A client-uploaded ``recovery_signals`` payload reading worse on each of the last
    ``days`` days, ending on ``end_date``.

    ``recovery_upload`` is deliberately flat: every scenario that carries it wants a
    reading that is merely present, not one that argues for anything, which is why nine
    committed scenarios share its unchanging 55/56 pair. This is the one scenario that
    needs the opposite -- readiness, HRV, sleep and resting heart rate all moving the
    same direction across several days, with no stated symptom anywhere in the turn.
    That shape, and not a single low value, is what issue #158's third condition asks a
    coach to read as evidence for pulling load rather than as one noisy reading to set
    aside.
    """
    end = dt.date.fromisoformat(end_date)
    readiness = (52.0, 41.0, 29.0)
    levels = ("MODERATE", "LOW", "LOW")
    hrv = ("BALANCED", "UNBALANCED", "UNBALANCED")
    resting_hr = (48.0, 53.0, 59.0)
    sleep = (68.0, 54.0, 41.0)
    count = min(days, len(readiness))
    return {
        "source": "client-upload:recovery",
        "days": [
            {
                "date": (end - dt.timedelta(days=count - 1 - offset)).isoformat(),
                "readiness_score": readiness[offset],
                "readiness_level": levels[offset],
                "hrv_status": hrv[offset],
                "hrv_7d_avg_ms": 70.0,
                "acute_load": 400.0,
                "recovery_time_sec": 3600.0,
                "body_battery_high": 60.0,
                "body_battery_low": 15.0,
                "avg_stress": 40.0,
                "resting_hr_bpm": resting_hr[offset],
                "sleep_score": sleep[offset],
            }
            for offset in range(count)
        ],
    }


def _http_error(url: str, status: int) -> urllib.error.HTTPError:
    """A synthetic upstream failure with no response body to read or close."""
    return urllib.error.HTTPError(url, status, "denied", None, None)


def endpoint_down(fake: FakeIntervals, url_fragment: str, status: int = 500) -> Callable:
    """``fake``, except every request whose URL contains ``url_fragment`` fails.

    The failing request is recorded before it fails, which is the one deliberate
    difference from the equivalent helper in ``test_gateway.py``. That one raises before
    delegating, so the failed call never reaches ``fake.calls`` and disappears from the
    request list entirely -- fine for a test that only reads the response body, wrong
    here, where the request list is half of what is being pinned. A request that reached
    the provider and came back 500 happened.
    """

    def wrapped(request: urllib.request.Request) -> ProviderResponse:
        fake.calls.append((request.get_method(), request.full_url))
        if url_fragment in request.full_url:
            raise _http_error(request.full_url, status)
        # Hand off to the real handler, whose first act is the same ``calls.append`` this
        # wrapper just made -- so drop ours rather than record the call twice.
        fake.calls.pop()
        return FakeIntervals.__call__(fake, request)

    return wrapped


# -- plan fixtures -----------------------------------------------------------------


def plan_with_execution(session_id: str, external_id: str) -> dict[str, Any]:
    """The example plan with one session set up to reconcile against a provider activity.

    Two edits, both needed. The ``execution`` block gives the session an owned external
    id for an activity to be paired with. Forcing ``match_status`` back to ``planned`` is
    what makes the session choosable at all: the example plan is authored as of Thursday,
    so its Monday, Tuesday and Wednesday sessions already read ``completed``, and
    reconciliation only ever considers a session still open. Reconciling an
    already-completed session is correctly a no-op, which would make a "reconciling"
    scenario quietly identical to its non-reconciling twin.
    """
    plan = publishable_plan()
    session = next(
        item for item in plan["week"]["sessions"] if item["session_id"] == session_id
    )
    session["execution"] = {
        "publish_supported": True,
        "external_id": external_id,
        "delivery_state": "intervals_accepted",
    }
    session["match_status"] = "planned"
    return plan


def plan_measuring_week_one_quality() -> dict[str, Any]:
    """The example plan whose cycle names a measurement, with the reference in week one.

    ``goal.measurement`` is optional and the example plan omits it, so every committed
    read so far reports ``measurement_evidence: null`` -- the honest answer for a cycle
    that scheduled no comparison, and an answer that never exercises the field. This is
    the other state: the cycle did name one, the reference session is the week-one
    quality run, and the week that repeats it is the last of the four.

    The reference sitting in week one is the point, not an incidental date. A reference
    is *meant* to be early -- it is the reading everything else is compared to -- so by
    the time the comparison is due it is always the oldest session in the cycle, and
    whatever a late-cycle read can still say about it is what a progress answer is made
    of.
    """
    plan = publishable_plan()
    plan["goal"]["measurement"] = {
        "reference_session_id": "run-quality-01",
        "measurement_week_start": "2026-08-31",
        "compare": "same route and effort, compare average heart rate",
    }
    return plan


def plan_with_two_sessions_on_the_quality_day() -> dict[str, Any]:
    """The example plan with an evening shakeout added beside the week-one quality run.

    One day, two sessions, one sport. It is an ordinary double day, and it is the only
    shape that produces ``activity_evidence: "other_activity_same_day"`` -- an activity
    on a day of that sport always attaches to *some* unclaimed session of that sport, so
    a session can only be left without one when a sibling took it. Every committed read
    before this had at most one session per day, which left that evidence state
    unreachable and therefore unread.

    What the state costs the coach is the interesting half: the day was trained and one
    of the two sessions was not, and which one it was is a question about the two
    prescriptions -- fifty minutes of intervals against twenty easy minutes -- not about
    the two session ids.
    """
    plan = publishable_plan()
    sessions = plan["week"]["sessions"]
    quality = next(item for item in sessions if item["session_id"] == "run-quality-01")
    shakeout = copy.deepcopy(
        next(item for item in sessions if item["session_id"] == "run-easy-01")
    )
    shakeout["session_id"] = "run-shakeout-01"
    shakeout["scheduled_date"] = quality["scheduled_date"]
    shakeout["purpose"] = "Loosen the legs in the evening after the quality session"
    shakeout["planned_minutes"] = 20
    shakeout["plan"] = {
        "kind": "time_axis",
        "name": "4km shakeout",
        "steps": [
            {
                "kind": "work",
                "name": "Easy run",
                "duration": {"kind": "distance", "meters": 4000},
                "target": {
                    "kind": "pace",
                    "unit": "sec_per_km",
                    "low_seconds_per_km": 420,
                    "high_seconds_per_km": 450,
                },
            }
        ],
    }
    # Rendered, never authored: the store refuses a prescription that is not this
    # module's output for the plan beside it, which is what makes the two agree.
    shakeout["prescription"] = render_prescription(shakeout["plan"])
    shakeout["match_status"] = "planned"
    shakeout["execution"] = {
        "publish_supported": True,
        "external_id": None,
        "delivery_state": "not_published",
    }
    sessions.insert(sessions.index(quality) + 1, shakeout)
    return plan


# -- stored-evidence fixtures ------------------------------------------------------
#
# Everything below is written through the product's own recording functions rather than
# by editing a file, so a scenario cannot pin a shape the product would never produce.


# Two sessions in the reviewed week, in the shape a training-log export arrives as. One
# lands on a day the provider also has an activity for and one does not, so the snapshot
# carries both answers to "did a device record this too" rather than only the easy one.
WEEK_REVIEW_UPLOAD_CSV = """Activity ID,Activity Date,Activity Name,Activity Type,Elapsed Time,Distance
7001,2026-08-11 06:40:00,Morning Run,Run,1800,4.2
7002,2026-08-15 07:10:00,Morning Run,Run,2400,5.6
"""


def seed_week_review_evidence(state_dir: Path) -> None:
    """What a week review reads besides the provider.

    Four different claims, because they are four different things and a review that
    cannot tell them apart says the wrong thing about all of them: a habit the athlete
    states, a lift they report on the day the plan prescribed it, a session no device
    recorded, and a file they uploaded. The last two are both "the athlete's word, not a
    provider actual" and are still not the same claim -- one is somebody remembering a
    session, the other is a device's own export arriving late -- which is why
    ``reported_activities`` carries where each row came from.
    """
    athlete_evidence.record_training_preference(
        state_dir,
        topic="quality_day",
        statement="習慣週五品質跑",
        now=dt.datetime(2026, 8, 15, 12, 0, tzinfo=dt.timezone.utc),
    )
    # Dated to the day the plan prescribes back squat, not to some other day of the same
    # week. That is the ordinary case, and it is the only one where the read can put what
    # was lifted beside what was asked for -- report it on the upper-body day instead and
    # ``movement_history`` carries the sets with nothing to compare them against.
    athlete_evidence.record_strength_report(
        state_dir,
        exercise="back squat",
        sets=[
            {"weight_kg": 70, "reps": 6},
            {"weight_kg": 70, "reps": 6},
            {"weight_kg": 70, "reps": 5},
        ],
        date="2026-08-10",
        now=dt.datetime(2026, 8, 10, 12, 0, tzinfo=dt.timezone.utc),
    )
    athlete_evidence.record_activity_summary(
        state_dir,
        sport="running",
        duration_minutes=25,
        date="2026-08-12",
        now=dt.datetime(2026, 8, 12, 18, 0, tzinfo=dt.timezone.utc),
    )
    reading = evidence_import.read_payload(
        format_name="csv", content=WEEK_REVIEW_UPLOAD_CSV
    )
    athlete_evidence.import_reported_evidence(
        state_dir,
        activities=reading["activities"],
        measurements=reading["measurements"],
        unreadable=reading["unreadable"],
        format_name=reading["format"],
        recognised_as=reading["recognised_as"],
        digest=reading["digest"],
        source_name="訓練紀錄匯出檔",
        now=dt.datetime(2026, 8, 16, 21, 0, tzinfo=dt.timezone.utc),
    )


def seed_today_statement(state_dir: Path) -> None:
    """One dated sentence, on the day the read is about.

    ``subjective_states`` is built into every context and, until this scenario, no read
    in this file produced a single row of it -- so no eval case could name it and nothing
    checked what an answer does with what the athlete said (issue #188). An empty group
    and a group with one sentence in it are two different starting states, and only the
    second one asks the coach to weigh it.

    One statement, because that is what a today-read normally has and the answer it
    supports reaches today and stops. The run of them is the next scenario.
    """
    athlete_evidence.record_subjective_state(
        state_dir,
        note="今天腿有點沉",
        date="2026-08-13",
        now=dt.datetime(2026, 8, 13, 7, 30, tzinfo=dt.timezone.utc),
    )


def seed_fortnight_of_statements(state_dir: Path) -> None:
    """The week review's own evidence, plus what the athlete said on five of its days.

    The same read as ``seed_week_review_evidence`` with one thing added, so the pair is a
    controlled comparison: what a review says differently when the athlete's own account
    of the fortnight is in front of it.

    Five statements across both calendar weeks, all saying a version of the same thing,
    against a wellness feed that reads within baseline throughout. That combination is
    the point -- it is the only shape where the sentences carry something no other field
    in the read carries and no deterministic reader will raise. One statement is a day;
    several across two weeks is a pattern, and telling those apart is the reading the
    field's own contract assigns to the coach.
    """
    seed_week_review_evidence(state_dir)
    for date, note, hour in (
        ("2026-08-05", "腿一直沒回來，輕鬆跑也覺得重", 21),
        ("2026-08-08", "睡滿八小時還是很累", 8),
        ("2026-08-11", "今天的輕鬆跑一點都不輕鬆", 20),
        ("2026-08-14", "腿還是沉的，這兩週都這樣", 7),
        ("2026-08-16", "這禮拜結束了，但完全沒有恢復的感覺", 19),
    ):
        athlete_evidence.record_subjective_state(
            state_dir,
            note=note,
            date=date,
            now=dt.datetime.fromisoformat(f"{date}T{hour:02d}:00:00+00:00"),
        )


def strength_alias_baseline(plan: dict[str, Any]) -> dict[str, Any]:
    """The example plan with its squat baseline carrying the athlete's own word.

    The live account's shape: a baseline established from the plan holds the canonical
    key and, in ``display_name``, what the athlete calls the lift. That alias is what
    lets a report said as 深蹲 anchor to the ``back squat`` baseline (issue #238). The
    pull-up baseline stays alias-less on purpose -- it is the control that shows a
    report no baseline names staying separate instead of being guessed onto one.
    """
    for load in plan["athlete_baseline"]["strength_loads"]:
        if load["exercise"] == "back squat":
            load["display_name"] = "深蹲"
    return plan


def seed_strength_alias_evidence(state_dir: Path) -> None:
    """One lift said three ways: the plan's key, the athlete's word, and a word
    nothing in the baseline carries.

    ``back squat`` and 深蹲 are the same lift twice -- once under the key a confirmed
    prescription stores, once as the athlete says it. With the baseline's
    ``display_name`` naming 深蹲 (``strength_alias_baseline``), both anchor to the same
    baseline entry and read back as one movement. 輔助引體 is the layer the resolver
    must not touch: the pull-up baseline carries no such word, matching it would be
    guessing the athlete's meaning, so it stays its own movement and the context says
    the names did not meet (issue #238's second layer).
    """
    athlete_evidence.record_strength_report(
        state_dir,
        exercise="back squat",
        sets=[{"weight_kg": 70, "reps": 6}, {"weight_kg": 70, "reps": 6}],
        date="2026-08-10",
        now=dt.datetime(2026, 8, 10, 19, 0, tzinfo=dt.timezone.utc),
    )
    athlete_evidence.record_strength_report(
        state_dir,
        exercise="深蹲",
        sets=[
            {"weight_kg": 72.5, "reps": 6},
            {"weight_kg": 72.5, "reps": 6},
            {"weight_kg": 72.5, "reps": 4},
        ],
        date="2026-08-12",
        notes=["最後一組少做兩下"],
        now=dt.datetime(2026, 8, 12, 19, 0, tzinfo=dt.timezone.utc),
    )
    athlete_evidence.record_strength_report(
        state_dir,
        exercise="輔助引體",
        sets=[{"assist_kg": 20, "reps": 8}, {"assist_kg": 20, "reps": 8}],
        date="2026-08-11",
        now=dt.datetime(2026, 8, 11, 19, 0, tzinfo=dt.timezone.utc),
    )


def seed_same_rollup_opposite_order(state_dir: Path) -> None:
    """One lift, twice, with byte-identical per-load arithmetic and opposite set order.

    Both sessions total fifteen repetitions at 70 kg and hold the load on every set, so
    ``load_rollup`` -- by_load, total, top load -- cannot tell them apart. What differs
    is the order: 08-05 opened weak and held the rest (3, 6, 6); 08-12 held until the
    last set collapsed (6, 6, 3). Opening slow and fading at the end are opposite
    readings of the same volume, and the direction lives only in the ordered sets the
    session record keeps. No note points at it on purpose: 08's fade case carries the
    athlete's own sentence, so this is the read with nothing but the order to go on.
    """
    athlete_evidence.record_strength_report(
        state_dir,
        exercise="back squat",
        sets=[
            {"weight_kg": 70, "reps": 3},
            {"weight_kg": 70, "reps": 6},
            {"weight_kg": 70, "reps": 6},
        ],
        date="2026-08-05",
        now=dt.datetime(2026, 8, 5, 19, 0, tzinfo=dt.timezone.utc),
    )
    athlete_evidence.record_strength_report(
        state_dir,
        exercise="back squat",
        sets=[
            {"weight_kg": 70, "reps": 6},
            {"weight_kg": 70, "reps": 6},
            {"weight_kg": 70, "reps": 3},
        ],
        date="2026-08-12",
        now=dt.datetime(2026, 8, 12, 19, 0, tzinfo=dt.timezone.utc),
    )


def seed_plan_authoring_evidence(state_dir: Path) -> None:
    """Everything an athlete has said that a plan-authoring turn reads back.

    A turn that writes next week, or the next cycle, starts from the same
    ``startCoachSession`` read as every other turn -- there is no separate authoring
    read. What distinguishes it is which stored statements have to survive the read:
    the week they said they lose, the aim past this cycle, the habit they stated, what
    they weigh, and what they last lifted. Those come from the athlete, never from a
    device, so a scenario that never records them cannot show whether the read still
    returns them.
    """
    recorded = dt.datetime(2026, 9, 3, 21, 0, tzinfo=dt.timezone.utc)
    athlete_evidence.record_availability(
        state_dir,
        week={
            "week_start": "2026-08-31",
            "available_days": ["mon", "tue", "thu", "sat", "sun"],
            "note": "週三週五出差，只帶了跑鞋",
        },
        now=recorded,
    )
    athlete_evidence.record_long_term_goal(
        state_dir,
        metric="半程馬拉松完賽時間",
        target="1:45",
        target_date="2027-03-01",
        now=recorded,
    )
    athlete_evidence.record_training_preference(
        state_dir,
        topic="quality_day",
        statement="習慣週五品質跑",
        now=recorded,
    )
    athlete_evidence.record_body_measurement(
        state_dir,
        weight_kg=72.5,
        body_fat_pct=18.0,
        date="2026-09-01",
        now=recorded,
    )
    athlete_evidence.record_strength_report(
        state_dir,
        exercise="back squat",
        sets=[
            {"weight_kg": 72.5, "reps": 6},
            {"weight_kg": 72.5, "reps": 6},
            {"weight_kg": 72.5, "reps": 5},
        ],
        date="2026-09-02",
        now=recorded,
    )
    athlete_evidence.record_subjective_state(
        state_dir,
        note="腿還是有點沉，但睡得比上週好",
        date="2026-09-03",
        now=recorded,
    )


def _easy_run_plan(kilometres: int, low_seconds: int, high_seconds: int) -> dict[str, Any]:
    """One continuous easy effort over a distance, at a pace band."""
    return {
        "kind": "time_axis",
        "name": f"{kilometres}km easy run",
        "steps": [
            {
                "kind": "work",
                "name": "Easy run",
                "duration": {"kind": "distance", "meters": kilometres * 1000},
                "target": {
                    "kind": "pace",
                    "unit": "sec_per_km",
                    "low_seconds_per_km": low_seconds,
                    "high_seconds_per_km": high_seconds,
                },
            }
        ],
    }


def _threshold_plan(repetitions: int, seconds_per_km: int) -> dict[str, Any]:
    """The cycle's threshold anchor at a repetition count and a pace.

    Same shape as the example plan's own quality session -- warm-up, the repeat, cool-down
    -- so a later week reads as a progression of one session rather than as a different
    kind of session appearing.
    """
    return {
        "kind": "time_axis",
        "name": f"{repetitions}x1000m threshold",
        "steps": [
            {
                "kind": "work",
                "name": "Warm-up",
                "duration": {"kind": "time", "seconds": 720},
                "target": {"kind": "open"},
            },
            {
                "kind": "repeat",
                "repetitions": repetitions,
                "steps": [
                    {
                        "kind": "work",
                        "name": "Interval",
                        "duration": {"kind": "distance", "meters": 1000},
                        "target": {
                            "kind": "pace",
                            "unit": "sec_per_km",
                            "low_seconds_per_km": seconds_per_km,
                            "high_seconds_per_km": seconds_per_km,
                        },
                    },
                    {
                        "kind": "work",
                        "name": "Jog recovery",
                        "duration": {"kind": "time", "seconds": 120},
                        "target": {"kind": "open"},
                    },
                ],
            },
            {
                "kind": "work",
                "name": "Cool-down",
                "duration": {"kind": "time", "seconds": 480},
                "target": {"kind": "open"},
            },
        ],
    }


def _repeat_session(template: dict[str, Any], session_id: str, date: str, **over: Any) -> dict[str, Any]:
    """One of week one's sessions, scheduled again in a later week of the same cycle.

    A cycle repeats its own shape -- that is what makes it a cycle -- so the weeks after
    the first are the first week's sessions on later dates, not new inventions. Anything
    that must not be carried forward is reset here: a delivery is a fact about one event
    and never travels, and neither does a measurement marker.
    """
    session = copy.deepcopy(template)
    session.update({"session_id": session_id, "scheduled_date": date, "match_status": "planned"})
    session["execution"] = {
        "publish_supported": True,
        "external_id": None,
        "delivery_state": "not_published",
    }
    session.pop("measures", None)
    session.update(over)
    session["prescription"] = render_prescription(session["plan"])
    return session


def roll_the_week_to_the_measurement_week(
    state_dir: Path, plan: dict[str, Any], now: dt.datetime
) -> None:
    """Walk the plan's one week forward to the fourth, one committed decision at a time.

    Every other scenario here leaves the plan on the week it was authored for, which is
    fine for a read taken inside that week and quietly wrong for a read taken three weeks
    later: the stored week still holds week one's sessions, so week one's prescriptions
    are in ``plan_state`` as well as in the cycle record, and a late-cycle read looks
    like it can see things it could not see in a plan that had been reviewed weekly.

    Here the week rolls the way a weekly review rolls it -- the outlook the plan already
    wrote for weeks two, three and four becoming precise one week at a time, and shrinking
    as it does. After the third roll, week one exists only in the commit chain, which is
    the shape a fourth-week review actually reads.
    """
    from garmin_coach_loop import validation as validation_module

    measurement = (plan.get("goal") or {}).get("measurement")
    measures = (
        {"measures": measurement["reference_session_id"]} if measurement is not None else {}
    )
    week_one = {session["session_id"]: session for session in plan["week"]["sessions"]}
    strength = week_one["strength-full-01"]
    easy = week_one["run-easy-01"]
    quality = week_one["run-quality-01"]
    upper = week_one["strength-upper-01"]
    long_run = week_one["run-long-01"]
    # The running sessions progress and the strength sessions do not, because that is
    # what this cycle's own outlook says: one repetition more, ten to fifteen minutes
    # longer, loads unchanged. It also decides what a later read can and cannot recover:
    # week one's 12 km long run and its five-repetition anchor are stated nowhere else in
    # the cycle, while its squat is stated in every week -- which is the difference
    # between a prescription that is lost when its row drops it and one that is not.
    weeks = [
        (
            "2026-08-17",
            "Extend the threshold anchor by one repetition",
            [
                _repeat_session(strength, "strength-full-02", "2026-08-17"),
                _repeat_session(easy, "run-easy-02", "2026-08-18"),
                _repeat_session(
                    quality, "run-quality-02", "2026-08-20", plan=_threshold_plan(6, 360)
                ),
                _repeat_session(upper, "strength-upper-02", "2026-08-21"),
                _repeat_session(
                    long_run, "run-long-02", "2026-08-23", plan=_easy_run_plan(13, 395, 425)
                ),
            ],
        ),
        (
            "2026-08-24",
            "Hold the volume and let the quality session carry the load",
            [
                _repeat_session(strength, "strength-full-03", "2026-08-24"),
                _repeat_session(easy, "run-easy-03", "2026-08-25"),
                _repeat_session(
                    quality, "run-quality-03", "2026-08-27", plan=_threshold_plan(6, 355)
                ),
                _repeat_session(upper, "strength-upper-03", "2026-08-28"),
                _repeat_session(
                    long_run, "run-long-03", "2026-08-30", plan=_easy_run_plan(14, 395, 425)
                ),
            ],
        ),
        (
            "2026-08-31",
            "Reduce volume and repeat the cycle's measurement",
            [
                _repeat_session(
                    easy, "run-easy-04", "2026-09-01", plan=_easy_run_plan(6, 390, 420)
                ),
                _repeat_session(strength, "strength-full-04", "2026-09-02"),
                # The comparison the cycle named, scheduled the way the product asks for
                # it: an ordinary session that also carries `measures`. Its plan is week
                # one's anchor unchanged, because a measurement that changed the
                # specification would not be measuring the same thing.
                _repeat_session(quality, "run-measure-01", "2026-09-03", **measures),
                _repeat_session(
                    easy, "run-easy-05", "2026-09-05", plan=_easy_run_plan(6, 390, 420)
                ),
            ],
        ),
    ]

    current = copy.deepcopy(plan)
    for start, intent, sessions in weeks:
        after = copy.deepcopy(current)
        after["version"] += 1
        after["week"] = {"start": start, "intent": intent, "sessions": sessions}
        # An outlined week that has become precise leaves the outlook (issue #61).
        after["cycle"]["outlook"] = after["cycle"]["outlook"][1:]
        # The commit boundary checks that the context projects the plan it was taken
        # against, so the projection has to come from the same place the check reads it
        # -- a hand-written copy here would drift into a fixture that pins a shape the
        # product refuses.
        context = copy.deepcopy(EXAMPLE_CONTEXT)
        context["goal_context"] = validation_module._expected_goal_context(current)
        context["current_calendar"] = validation_module._expected_current_calendar(current)
        context["athlete_baseline"] = validation_module._expected_context_baseline(current)
        # The key is always there; it carries a value exactly when the cycle named a
        # measurement, which is what the boundary checks. `null` is a real state -- this
        # cycle scheduled no comparison -- and not the same as the key being absent.
        context["measurement_evidence"] = (
            {
                "comparison_session_id": None,
                "reference_result": "not_in_record",
                "comparison_result": "not_scheduled",
            }
            if measurement is not None
            else None
        )
        event = copy.deepcopy(EXAMPLE_EVENT)
        event.update(
            {
                "mode": "plan_week",
                "action": "adjust",
                "session_id": None,
                "event_id": f"fixture-week-roll-{start}",
                "created_at": f"{start}T07:00:00+08:00",
                "plan_version_before": current["version"],
                "plan_version_after": after["version"],
            }
        )
        store_module.apply_decision(state_dir, context=context, after=after, event=event)
        current = after


def roll_the_week_through_a_quality_series_that_concedes_once(
    state_dir: Path, plan: dict[str, Any], now: dt.datetime
) -> None:
    """Walk the plan's threshold anchor through three completed steps and one that
    does not finish.

    Same mechanics as ``roll_the_week_to_the_measurement_week`` -- the outlook becoming
    precise one week at a time, each roll a real committed decision, so every earlier
    week's prescription lives only in the commit chain by the time this reads. What
    differs is which question the four weeks are built to answer.

    Issue #255: ``movement_history`` pivots strength by movement, so a coach opening
    the next squat session reads the last several loads and reps in one place. Running
    has no equivalent -- ``cycle_sessions`` is one flat list across every sport in the
    cycle, in date order, and whether four of its rows are the same session family
    repeating is something only a reader who parses every prescription string can
    tell. This is that family, laid out so the question is answerable at all: five
    reps at 360 seconds per kilometre, six at 360, six at 355 -- one repetition or one
    pace step harder each week, every one of them an activity within a couple of
    minutes of what its week asked for. The fourth keeps climbing the way the first
    three earned -- seven reps, pace held at 355 -- and the activity that day stops at
    barely half the prescribed time, well under even the first week's easier session.

    Nothing here says why. There is no subjective note and no red flag, only the
    numbers, because the question this scenario exists to put to a coaching turn is
    exactly the one AGENTS.md 4 and 5 leave to it: whether one short exposure against
    a three-week rising pattern reads as a reason to back off, or as one data point a
    stable trend outweighs. Shading the fixture with a stated reason for the short
    session would answer that question in the fixture instead of leaving it to the
    read.
    """
    from garmin_coach_loop import validation as validation_module

    week_one = {session["session_id"]: session for session in plan["week"]["sessions"]}
    strength = week_one["strength-full-01"]
    easy = week_one["run-easy-01"]
    quality = week_one["run-quality-01"]
    upper = week_one["strength-upper-01"]
    long_run = week_one["run-long-01"]
    # The quality session is the only one this scenario is about. Strength, the easy
    # day and the long run keep the same weekly shape every week -- a real cycle would
    # move them too, but moving them would give a reader another axis to explain the
    # fourth week's short session by, and this scenario exists to isolate the one axis
    # that matters: what this family asked for, and what came back for it, four times.
    weeks = [
        (
            "2026-08-17",
            "Extend the threshold anchor by one repetition",
            [
                _repeat_session(strength, "strength-full-02", "2026-08-17"),
                _repeat_session(easy, "run-easy-02", "2026-08-18"),
                _repeat_session(
                    quality, "run-quality-02", "2026-08-20",
                    plan=_threshold_plan(6, 360), planned_minutes=55,
                ),
                _repeat_session(upper, "strength-upper-02", "2026-08-21"),
                _repeat_session(
                    long_run, "run-long-02", "2026-08-23", plan=_easy_run_plan(13, 395, 425)
                ),
            ],
        ),
        (
            "2026-08-24",
            "Hold the repetition count and bring the anchor pace down",
            [
                _repeat_session(strength, "strength-full-03", "2026-08-24"),
                _repeat_session(easy, "run-easy-03", "2026-08-25"),
                _repeat_session(
                    quality, "run-quality-03", "2026-08-27",
                    plan=_threshold_plan(6, 355), planned_minutes=58,
                ),
                _repeat_session(upper, "strength-upper-03", "2026-08-28"),
                _repeat_session(
                    long_run, "run-long-03", "2026-08-30", plan=_easy_run_plan(14, 395, 425)
                ),
            ],
        ),
        (
            "2026-08-31",
            "Extend the threshold anchor by one more repetition",
            [
                _repeat_session(strength, "strength-full-04", "2026-08-31"),
                _repeat_session(easy, "run-easy-04", "2026-09-01"),
                _repeat_session(
                    quality, "run-quality-04", "2026-09-03",
                    plan=_threshold_plan(7, 355), planned_minutes=62,
                ),
                _repeat_session(upper, "strength-upper-04", "2026-09-04"),
                _repeat_session(
                    long_run, "run-long-04", "2026-09-06", plan=_easy_run_plan(15, 395, 425)
                ),
            ],
        ),
    ]

    current = copy.deepcopy(plan)
    for start, intent, sessions in weeks:
        after = copy.deepcopy(current)
        after["version"] += 1
        after["week"] = {"start": start, "intent": intent, "sessions": sessions}
        after["cycle"]["outlook"] = after["cycle"]["outlook"][1:]
        context = copy.deepcopy(EXAMPLE_CONTEXT)
        context["goal_context"] = validation_module._expected_goal_context(current)
        context["current_calendar"] = validation_module._expected_current_calendar(current)
        context["athlete_baseline"] = validation_module._expected_context_baseline(current)
        # This plan names no measurement, unlike ``plan_measuring_week_one_quality``'s --
        # always null rather than the conditional the other roll uses, because there is
        # only the one state to state.
        context["measurement_evidence"] = None
        event = copy.deepcopy(EXAMPLE_EVENT)
        event.update(
            {
                "mode": "plan_week",
                "action": "adjust",
                "session_id": None,
                "event_id": f"fixture-quality-series-{start}",
                "created_at": f"{start}T07:00:00+08:00",
                "plan_version_before": current["version"],
                "plan_version_after": after["version"],
            }
        )
        store_module.apply_decision(state_dir, context=context, after=after, event=event)
        current = after


def roll_the_week_to_an_unplanned_strength_pair(
    state_dir: Path, plan: dict[str, Any], now: dt.datetime
) -> None:
    """Walk the plan two weeks forward, then stop authoring strength for the third.

    Same mechanics as the other two ``roll_the_week_*`` helpers -- the outlook becoming
    precise one committed decision at a time, so weeks one and two exist only in the
    commit chain by the time this reads, exactly what a plan reviewed every Monday would
    leave behind. What differs is the third week's own session list, and why it is
    shaped that way.

    Weeks one and two repeat the cycle's ordinary shape unchanged -- Monday and Friday
    strength, Tuesday easy, Thursday quality, Sunday long, loads held flat both times --
    so a plan_week turn reading this history can see the pattern it might be asked to
    continue into week four (issue #256's eval needs "carry the structure forward" to be
    a legible option, not the thing under test). Week three's authored sessions keep the
    three running days and drop both strength entries.

    That drop is deliberate and is the only way this fixture can measure issue #256 at
    all. ``_match_actuals_to_plan`` (context_core.py) links a provider activity to *any*
    session anywhere in the cycle's committed history that shares its exact date and
    sport -- not only to one still in the live week -- and the moment it finds one,
    ``_apply_planned_classification`` overwrites the activity's ``body_stress`` and
    ``cost`` with that session's own authored values. Week one's own
    ``strength-full-01`` and ``strength-upper-01`` already carry different
    ``body_stress`` values (``full`` vs ``upper``) -- so a strength activity that matched
    its own planned session would arrive already distinguishable, and never exercise the
    unmatched path issue #256 is about. Only an activity with zero same-date, same-sport
    candidates anywhere in cycle_sessions reaches the response as the source module built
    it, which is why week three is never given a strength session to match against.
    """
    from garmin_coach_loop import validation as validation_module

    week_one = {session["session_id"]: session for session in plan["week"]["sessions"]}
    strength = week_one["strength-full-01"]
    easy = week_one["run-easy-01"]
    quality = week_one["run-quality-01"]
    upper = week_one["strength-upper-01"]
    long_run = week_one["run-long-01"]

    weeks = [
        (
            "2026-08-17",
            "Repeat the week's rhythm and hold every load steady",
            [
                _repeat_session(strength, "strength-full-02", "2026-08-17"),
                _repeat_session(easy, "run-easy-02", "2026-08-18"),
                _repeat_session(quality, "run-quality-02", "2026-08-20"),
                _repeat_session(upper, "strength-upper-02", "2026-08-21"),
                _repeat_session(long_run, "run-long-02", "2026-08-23"),
            ],
        ),
        (
            "2026-08-24",
            "Hold the running rhythm; strength continues on the athlete's own schedule",
            [
                _repeat_session(easy, "run-easy-03", "2026-08-25"),
                _repeat_session(quality, "run-quality-03", "2026-08-27"),
                _repeat_session(long_run, "run-long-03", "2026-08-30"),
            ],
        ),
    ]

    current = copy.deepcopy(plan)
    for start, intent, sessions in weeks:
        after = copy.deepcopy(current)
        after["version"] += 1
        after["week"] = {"start": start, "intent": intent, "sessions": sessions}
        after["cycle"]["outlook"] = after["cycle"]["outlook"][1:]
        context = copy.deepcopy(EXAMPLE_CONTEXT)
        context["goal_context"] = validation_module._expected_goal_context(current)
        context["current_calendar"] = validation_module._expected_current_calendar(current)
        context["athlete_baseline"] = validation_module._expected_context_baseline(current)
        # This plan names no measurement, same as the quality-series roll beside this
        # one -- always null rather than a conditional, because there is only the one
        # state to state.
        context["measurement_evidence"] = None
        event = copy.deepcopy(EXAMPLE_EVENT)
        event.update(
            {
                "mode": "plan_week",
                "action": "adjust",
                "session_id": None,
                "event_id": f"fixture-strength-pair-{start}",
                "created_at": f"{start}T07:00:00+08:00",
                "plan_version_before": current["version"],
                "plan_version_after": after["version"],
            }
        )
        store_module.apply_decision(state_dir, context=context, after=after, event=event)
        current = after


def seed_unplanned_strength_pair_evidence(state_dir: Path) -> None:
    """What the athlete said about the two strength days no plan session claims.

    Two lifts each day -- a main movement and one accessory -- exactly the shape
    ``recordStrengthExecution`` accepts one call per movement. The heavy day names the
    same ``back squat`` key the athlete's baseline already carries; the easy day names
    ``bench press``, the baseline's other maintained lift but at a light, unconfirmed
    working weight. Nothing here states a subjective_feel: ``record_strength_report``
    has no such field and never has one (see its docstring). The only subjective_feel a
    strength actual can carry comes from the provider's own ``feel`` on the matched
    activity -- set on the two rows this scenario hands ``FakeIntervals`` -- which is a
    different container, joined to this one only by date. That split is exactly what
    issue #257's timeline proposal is about.
    """
    athlete_evidence.record_strength_report(
        state_dir,
        exercise="back squat",
        sets=[{"weight_kg": 80, "reps": 5} for _ in range(5)],
        date="2026-08-24",
        now=dt.datetime(2026, 8, 24, 19, 0, tzinfo=dt.timezone.utc),
    )
    athlete_evidence.record_strength_report(
        state_dir,
        exercise="羅馬尼亞硬舉",
        sets=[{"weight_kg": 50, "reps": 10} for _ in range(3)],
        date="2026-08-24",
        now=dt.datetime(2026, 8, 24, 19, 5, tzinfo=dt.timezone.utc),
    )
    athlete_evidence.record_strength_report(
        state_dir,
        exercise="bench press",
        sets=[{"weight_kg": 40, "reps": 8} for _ in range(3)],
        date="2026-08-28",
        now=dt.datetime(2026, 8, 28, 19, 0, tzinfo=dt.timezone.utc),
    )
    athlete_evidence.record_strength_report(
        state_dir,
        exercise="槓鈴划船",
        sets=[{"weight_kg": 35, "reps": 10} for _ in range(3)],
        date="2026-08-28",
        now=dt.datetime(2026, 8, 28, 19, 5, tzinfo=dt.timezone.utc),
    )


def open_unresolved_delivery(state_dir: Path, plan: dict[str, Any], now: dt.datetime) -> None:
    """A reservation an interrupted delivery left behind.

    While it stands, every PlanState commit is fenced -- and reconciliation is made of
    commits, so the read defers rather than attempts it. Deferral is only worth pinning
    on a read that would otherwise have applied something, which is why the scenario
    using this also hands the provider a matching activity.

    The reservation writer takes no clock, and correctly so: it is called from inside a
    delivery, where the only honest answer to "when did this start" is the wall clock.
    That leaves this fixture with the one timestamp in these scenarios that a second run
    would not reproduce -- and the reservation is reported back in ``delivery``, so it
    would make this snapshot differ from itself every time. Pinning it here is the
    narrowest fix available without changing the product. Naming the helper directly is
    deliberate: if it is ever renamed this raises immediately, which is the loud failure,
    rather than quietly leaving the timestamp live again.
    """
    stamp = now.astimezone(dt.timezone.utc).replace(microsecond=0).isoformat().replace(
        "+00:00", "Z"
    )
    with mock.patch.object(store_module, "_utc_stamp", lambda: stamp):
        store_module.open_delivery_attempt(
            state_dir,
            kind="delivery",
            plan_id=plan["plan_id"],
            plan_version=plan["version"],
            proposal_hash="a" * 64,
            operations=[
                {
                    "session_id": "run-quality-01",
                    "operation": "upsert",
                    "owned_external_id": "ev-quality-pending",
                    "external_id": None,
                    "scheduled_date": "2026-08-13",
                }
            ],
        )


# -- the scenarios ------------------------------------------------------------------


@dataclass(frozen=True)
class Scenario:
    """One read, and everything needed to reproduce it byte for byte.

    ``modes`` names the DecisionEvent modes of the coaching turns this read begins -- the
    same vocabulary ``evals/cases`` uses. It is a set rather than one value because
    ``start_session`` takes no mode and returns the same shape whichever turn follows: a
    read taken near a cycle boundary, with everything the athlete has stated on record, is
    equally the read a week-authoring turn and a cycle-authoring turn begin from. Naming
    both is what lets a declared eval case be matched to a read that can actually answer
    it, and it never weakens the match: every path a case declares still has to resolve.
    """

    name: str
    modes: tuple[str, ...]
    purpose: str
    now: dt.datetime
    plan: Callable[[], dict[str, Any]] | None
    body: dict[str, Any] = field(default_factory=dict)
    configure_fake: Callable[[FakeIntervals], None] | None = None
    seed_store: Callable[[Path, dict[str, Any], dt.datetime], None] | None = None
    seed_evidence: Callable[[Path], None] | None = None
    wrap_fetch: Callable[[FakeIntervals], Callable] | None = None


def _with_run_settings(fake: FakeIntervals) -> None:
    """Answer ``/sport-settings`` instead of refusing it.

    ``FakeIntervals`` defaults to a 403 so every fallback stays covered. A scenario whose
    plan carries a measured max heart rate does get that request made, so it has to have
    something to answer with, or the snapshot pins an outage rather than a read.
    """
    fake.sport_settings = copy.deepcopy(RUN_SPORT_SETTINGS)


def _configure(*steps: Callable[[FakeIntervals], None]) -> Callable[[FakeIntervals], None]:
    def apply(fake: FakeIntervals) -> None:
        for step in steps:
            step(fake)

    return apply


def _activities(*rows: dict[str, Any]) -> Callable[[FakeIntervals], None]:
    def apply(fake: FakeIntervals) -> None:
        fake.activities = [copy.deepcopy(row) for row in rows]

    return apply


def _segments(
    activity_id: str, *rows: dict[str, Any]
) -> Callable[[FakeIntervals], None]:
    """Teach the fake what one activity's breakdown looks like.

    Without this the per-segment read still fires and comes back empty, which is a
    real provider answer -- and was, until this existed, the only answer any committed
    read here had ever been taken against.
    """

    def apply(fake: FakeIntervals) -> None:
        fake.segments_by_activity[activity_id] = [copy.deepcopy(row) for row in rows]

    return apply


def _wellness(rows: list[dict[str, Any]]) -> Callable[[FakeIntervals], None]:
    def apply(fake: FakeIntervals) -> None:
        fake.wellness = copy.deepcopy(rows)

    return apply


# The plan's own quality session is `5x1000m threshold`: a 12-minute warm-up, five
# 1000-metre repetitions at 6:00/km with two-minute jogs between them, and an 8-minute
# cool-down. The two breakdowns below are the two ways that session comes back from a
# watch, and neither one is readable from the whole-activity average that
# `recent_actuals` carries.
#
# Outdoors, three of the five repetitions were run and the session ended early. The
# warm-up arrives split across two segments -- the live shape, not an invention -- so
# the count of segments is not the count of prescribed steps and never was.
QUALITY_SEGMENTS_THREE_OF_FIVE = (
    segment_row(seconds=415, meters=965, hr=132),
    segment_row(seconds=305, meters=718, hr=141),
    segment_row(seconds=358, meters=1000, hr=168, max_hr=174, min_hr=149),
    segment_row(seconds=120, meters=261, hr=152),
    segment_row(seconds=363, meters=1000, hr=171, max_hr=177, min_hr=152),
    segment_row(seconds=120, meters=255, hr=154),
    segment_row(seconds=379, meters=1000, hr=174, max_hr=179, min_hr=155),
    segment_row(seconds=122, meters=254, hr=150),
    segment_row(seconds=240, meters=527, hr=136),
    segment_row(seconds=218, meters=464, hr=130),
)

# Indoors, every repetition was completed. The heart rates are a threshold session's;
# the paces are not, because a treadmill's distance is the machine's reading rather
# than a measurement -- on this athlete's device roughly a fifth short, which turns a
# 6:00/km repetition into a 7:30/km one on the record and stretches a 60-minute
# session to 67. Whether that reads as a missed target or as an uncomparable
# measurement is issue #252's second half.
QUALITY_SEGMENTS_INDOOR_ALL_FIVE = (
    segment_row(seconds=400, meters=755, hr=130),
    segment_row(seconds=320, meters=610, hr=140),
    segment_row(seconds=450, meters=1000, hr=168, max_hr=174, min_hr=149),
    segment_row(seconds=120, meters=218, hr=150),
    segment_row(seconds=452, meters=1000, hr=171, max_hr=177, min_hr=152),
    segment_row(seconds=120, meters=216, hr=152),
    segment_row(seconds=449, meters=1000, hr=173, max_hr=178, min_hr=154),
    segment_row(seconds=120, meters=215, hr=154),
    segment_row(seconds=451, meters=1000, hr=175, max_hr=180, min_hr=155),
    segment_row(seconds=120, meters=214, hr=155),
    segment_row(seconds=448, meters=1000, hr=177, max_hr=182, min_hr=157),
    segment_row(seconds=120, meters=213, hr=156),
    segment_row(seconds=480, meters=845, hr=138),
)

# Outdoors, every repetition completed, at close to the prescribed 6:00/km. This is the
# comparison end of a cycle's own measurement, and it is deliberately a different set of
# numbers from the three-of-five breakdown above: no repetition time and no segment
# distance appears in both, so a figure an answer states about the older session cannot
# be scored as supported by the newer one. The two whole-activity averages -- 152 bpm
# over 44 minutes against 154 bpm over 60 -- are what a coach is left comparing when the
# older session's repetitions are no longer in the read, and they are not two readings of
# the same session (issue #290).
QUALITY_SEGMENTS_FIVE_OF_FIVE = (
    segment_row(seconds=428, meters=1000, hr=128),
    segment_row(seconds=292, meters=700, hr=138),
    segment_row(seconds=356, meters=1000, hr=164, max_hr=170, min_hr=145),
    segment_row(seconds=119, meters=265, hr=150),
    segment_row(seconds=357, meters=1000, hr=167, max_hr=173, min_hr=148),
    segment_row(seconds=121, meters=268, hr=152),
    segment_row(seconds=359, meters=1000, hr=169, max_hr=175, min_hr=150),
    segment_row(seconds=123, meters=263, hr=153),
    segment_row(seconds=361, meters=1000, hr=172, max_hr=178, min_hr=151),
    segment_row(seconds=124, meters=259, hr=154),
    segment_row(seconds=364, meters=1000, hr=175, max_hr=181, min_hr=153),
    segment_row(seconds=126, meters=257, hr=152),
    segment_row(seconds=470, meters=1070, hr=132),
)


def scenarios() -> list[Scenario]:
    """Every read this regression covers, in the order the snapshots are written.

    The list is paired on purpose. Six of the reads appear twice, once where
    reconciliation has nothing to apply and once where it applies: applying rewrites the
    plan and rebuilds the context against the moved plan, and that rebuild is the half of
    the read that a change to how often the provider is called would most easily break
    -- a rebuild that re-fetched would double every request, and a rebuild that reused a
    stale domain would answer half the response from a different moment.
    """
    return [
        # ---- today's session -------------------------------------------------------
        Scenario(
            name="01_revisit_today__no_reconcile",
            modes=("revisit_today",),
            purpose=(
                "Today's session on a day with a client recovery upload and stated "
                "available days, with no provider activity to match"
            ),
            now=NOW_TODAY,
            plan=publishable_plan,
            body={
                "recovery_signals": recovery_upload("2026-08-13"),
                "available_days": ["mon", "tue", "wed", "thu", "fri"],
            },
            configure_fake=_configure(_with_run_settings, _activities()),
        ),
        Scenario(
            name="01_revisit_today__reconcile",
            modes=("revisit_today",),
            purpose=(
                "The same read where Monday's easy run pairs with its delivered event, "
                "so the plan moves and the context is rebuilt against it"
            ),
            now=NOW_TODAY,
            plan=lambda: plan_with_execution("run-easy-01", "ev-easy-01"),
            body={"recovery_signals": recovery_upload("2026-08-13")},
            configure_fake=_configure(
                _with_run_settings,
                _activities(
                    activity_row(
                        "i-easy-01", "2026-08-11", minutes=30, distance_m=4200,
                        avg_speed=2.33, hr=149, paired_event_id="ev-easy-01",
                    )
                ),
            ),
        ),
        # ---- week review -----------------------------------------------------------
        Scenario(
            name="02_review_week__no_reconcile",
            modes=("review_week",),
            purpose=(
                "The Monday after the plan's stored week, with a full wellness feed, a "
                "stated habit, a reported lift and a session no device recorded"
            ),
            now=NOW_WEEK_REVIEW,
            plan=publishable_plan,
            body={
                "recovery_signals": recovery_upload("2026-08-17"),
                "available_days": ["mon", "tue", "wed", "thu", "fri"],
                "schedule_changed": True,
                "equipment_changed": False,
            },
            configure_fake=_configure(
                _with_run_settings,
                _wellness(wellness_rows("2026-08-17")),
                _activities(
                    activity_row(
                        "i-review-01", "2026-08-11", minutes=30, distance_m=4200,
                        avg_speed=2.33, hr=149,
                    )
                ),
            ),
            seed_evidence=seed_week_review_evidence,
        ),
        Scenario(
            name="02_review_week__reconcile",
            modes=("review_week",),
            purpose="The same week review where Sunday's long run pairs and the plan moves",
            now=NOW_WEEK_REVIEW,
            plan=lambda: plan_with_execution("run-long-01", "ev-long-01"),
            body={"recovery_signals": recovery_upload("2026-08-17")},
            configure_fake=_configure(
                _with_run_settings,
                _wellness(wellness_rows("2026-08-17")),
                _activities(
                    activity_row(
                        "i-long-01", "2026-08-16", minutes=55, distance_m=12000,
                        avg_speed=3.33, hr=140, paired_event_id="ev-long-01",
                    )
                ),
            ),
            seed_evidence=seed_week_review_evidence,
        ),
        # ---- cycle review ----------------------------------------------------------
        Scenario(
            name="03_review_cycle__no_reconcile",
            modes=("review_cycle",),
            purpose=(
                "Day 26 of the 28-day cycle, reading the whole cycle back with one "
                "unmatched provider activity in it"
            ),
            now=NOW_CYCLE_REVIEW,
            plan=publishable_plan,
            body={},
            configure_fake=_configure(
                _with_run_settings,
                _activities(
                    activity_row(
                        "i-cycle-01", "2026-08-20", minutes=40, distance_m=8000,
                        avg_speed=3.33, hr=155,
                    )
                ),
            ),
        ),
        Scenario(
            name="03_review_cycle__reconcile",
            modes=("review_cycle",),
            purpose="The same cycle review where a session inside the cycle pairs and the plan moves",
            now=NOW_CYCLE_REVIEW,
            plan=lambda: plan_with_execution("run-long-01", "ev-long-01-cycle"),
            body={},
            configure_fake=_configure(
                _with_run_settings,
                _activities(
                    activity_row(
                        "i-long-02", "2026-08-16", minutes=55, distance_m=12000,
                        avg_speed=3.33, hr=140, paired_event_id="ev-long-01-cycle",
                    )
                ),
            ),
        ),
        # ---- a structured run ------------------------------------------------------
        Scenario(
            name="04_structured_run__no_reconcile",
            modes=("revisit_today",),
            purpose=(
                "Today's session is the one the plan prescribes more than one step for, "
                "with an activity on the day that is not paired to it"
            ),
            now=NOW_TODAY,
            plan=publishable_plan,
            body={},
            configure_fake=_configure(
                _with_run_settings,
                _activities(
                    activity_row(
                        "i-quality-solo", "2026-08-13", minutes=30, distance_m=6000,
                        avg_speed=3.33, hr=165,
                    )
                ),
            ),
        ),
        Scenario(
            name="04_structured_run__reconcile",
            modes=("revisit_today",),
            purpose=(
                "The multi-step session pairs with its own activity, and the "
                "per-segment read comes back with three of the five prescribed "
                "repetitions run and the session ended early"
            ),
            now=NOW_TODAY,
            plan=lambda: plan_with_execution("run-quality-01", "ev-quality-01"),
            body={},
            configure_fake=_configure(
                _with_run_settings,
                _activities(
                    # The whole-activity figures are the sum of the segments below, so
                    # the two readings of one session cannot contradict each other. They
                    # are also the reason the segments are worth reading: 44 minutes
                    # averaging 6:50/km is what a completed easy run looks like, and this
                    # was three quarters of a threshold session.
                    activity_row(
                        "i-quality-01", "2026-08-13", minutes=44, distance_m=6444,
                        avg_speed=2.441, hr=152, paired_event_id="ev-quality-01",
                    )
                ),
                _segments("i-quality-01", *QUALITY_SEGMENTS_THREE_OF_FIVE),
            ),
        ),
        # ---- sparse and failed recovery reads ---------------------------------------
        Scenario(
            name="05_sparse_recovery__values_mostly_absent",
            modes=("revisit_today",),
            purpose=(
                "Wellness rows the provider answered for but filled in almost nothing "
                "on -- a read with no reading, which is not a bad reading"
            ),
            now=NOW_TODAY,
            plan=publishable_plan,
            body={},
            configure_fake=_configure(
                _with_run_settings, _wellness(sparse_wellness_rows("2026-08-13"))
            ),
        ),
        # The paired scenario to "10", and the pair is the point. The same outage now
        # costs neither account its turn: the one with no plan never read wellness at
        # all, and the one with a plan answers with its recovery half stated as unread --
        # freshness "unknown", empty coverage, and the failed read named in unknowns. The
        # asymmetry this pair was blessed to hold is gone, which is what AGENTS.md 3 asked
        # for; what the snapshot holds now is that the unread half never quietly starts
        # reading as a measured one.
        Scenario(
            name="06_recovery_read_fails",
            modes=("revisit_today",),
            purpose=(
                "The wellness endpoint answers 500 on an account that has a plan: the "
                "turn still answers, with recovery unread rather than measured"
            ),
            now=NOW_TODAY,
            plan=publishable_plan,
            body={},
            configure_fake=_with_run_settings,
            wrap_fetch=lambda fake: endpoint_down(fake, "/wellness?"),
        ),
        # ---- a reported symptom -----------------------------------------------------
        Scenario(
            name="07_red_flag__no_reconcile",
            modes=("revisit_today",),
            purpose="Today's read with a symptom reported true, and nothing to reconcile",
            now=NOW_TODAY,
            plan=publishable_plan,
            # The recovery upload is here on purpose. A symptom arriving on a day with
            # good-looking readiness numbers is the read this scenario stands for: the
            # two are beside each other in one context, and the answer is not allowed to
            # let the number settle the symptom.
            body={
                "red_flags": {"chest_pain": True, "dizziness": True},
                "recovery_signals": recovery_upload("2026-08-13"),
            },
            configure_fake=_configure(_with_run_settings, _activities()),
        ),
        Scenario(
            name="07_red_flag__reconcile",
            modes=("revisit_today",),
            purpose=(
                "The same symptom read where a session also pairs -- a reported symptom "
                "must not change what the read is allowed to reconcile"
            ),
            now=NOW_TODAY,
            plan=lambda: plan_with_execution("run-easy-01", "ev-easy-01-flag"),
            body={
                "red_flags": {"chest_pain": True, "dizziness": True},
                "recovery_signals": recovery_upload("2026-08-13"),
            },
            configure_fake=_configure(
                _with_run_settings,
                _activities(
                    activity_row(
                        "i-easy-flag", "2026-08-11", minutes=30, distance_m=4200,
                        avg_speed=2.33, hr=149, paired_event_id="ev-easy-01-flag",
                    )
                ),
            ),
        ),
        # ---- one lift under two keys -------------------------------------------------
        Scenario(
            name="08_strength_alias__no_reconcile",
            modes=("review_week",),
            purpose=(
                "One lift under the plan's key and the athlete's own word reads back "
                "as one movement; a word no baseline names stays separate and is said"
            ),
            now=NOW_TODAY,
            plan=lambda: strength_alias_baseline(publishable_plan()),
            body={},
            configure_fake=_configure(_with_run_settings, _activities()),
            seed_evidence=seed_strength_alias_evidence,
        ),
        Scenario(
            name="08_strength_alias__reconcile",
            modes=("review_week",),
            purpose="The same aliases, on a read that also reconciles and rebuilds",
            now=NOW_TODAY,
            plan=lambda: strength_alias_baseline(
                plan_with_execution("run-easy-01", "ev-easy-01-strength")
            ),
            body={},
            configure_fake=_configure(
                _with_run_settings,
                _activities(
                    activity_row(
                        "i-easy-strength", "2026-08-11", minutes=30, distance_m=4200,
                        avg_speed=2.33, hr=149, paired_event_id="ev-easy-01-strength",
                    )
                ),
            ),
            seed_evidence=seed_strength_alias_evidence,
        ),
        # ---- no plan yet --------------------------------------------------------------
        Scenario(
            name="09_no_plan__provider_healthy",
            modes=("plan_cycle",),
            purpose=(
                "An account with no PlanState and a working provider: the first "
                "conversation reads the training already on record instead of asking for it"
            ),
            now=NOW_TODAY,
            plan=None,
            body={},
            configure_fake=_activities(
                activity_row("i9001", "2026-08-11", minutes=35, distance_m=7000, avg_speed=3.33)
            ),
        ),
        Scenario(
            name="10_no_plan__recovery_read_fails",
            modes=("plan_cycle",),
            purpose=(
                "The same empty account with the wellness endpoint down -- the history "
                "the first conversation exists to avoid re-asking must survive it"
            ),
            now=NOW_TODAY,
            plan=None,
            body={},
            configure_fake=_activities(
                activity_row("i9001", "2026-08-11", minutes=35, distance_m=7000, avg_speed=3.33)
            ),
            wrap_fetch=lambda fake: endpoint_down(fake, "/wellness?"),
        ),
        # ---- a delivery that did not finish -------------------------------------------
        Scenario(
            name="11_unresolved_delivery__reconciliation_deferred",
            modes=("revisit_today",),
            purpose=(
                "An interrupted delivery's reservation fences the store, so a read that "
                "had a session to reconcile defers instead of writing"
            ),
            now=NOW_TODAY,
            plan=lambda: plan_with_execution("run-easy-01", "ev-easy-01-fenced"),
            body={},
            configure_fake=_configure(
                _with_run_settings,
                _activities(
                    activity_row(
                        "i-easy-fenced", "2026-08-11", minutes=30, distance_m=4200,
                        avg_speed=2.33, hr=149, paired_event_id="ev-easy-01-fenced",
                    )
                ),
            ),
            seed_store=open_unresolved_delivery,
        ),
        # ---- the read a plan-authoring turn begins from -------------------------------
        #
        # The one scenario the throwaway comparison had no equivalent of, and the reason
        # seven of its eighteen eval cases could not be bound to any read. A turn that
        # writes next week, or the next cycle, is not a different call: it begins from the
        # same ``startCoachSession``. What makes it a different *read* is which stored
        # statements have to come back in it -- the ones the athlete made and no device
        # recorded. So the scenario is that read, at the moment both turns happen: late in
        # the cycle, with the week ahead not yet written.
        Scenario(
            name="12_plan_authoring__stated_evidence",
            modes=("plan_week", "plan_cycle"),
            purpose=(
                "Near the cycle boundary, with everything the athlete has stated on "
                "record: a lost week, an aim past this cycle, a habit, a weight, a lift"
            ),
            now=NOW_CYCLE_REVIEW,
            plan=publishable_plan,
            body={
                "recovery_signals": recovery_upload("2026-09-04"),
                "available_days": ["mon", "tue", "thu", "sat", "sun"],
                "schedule_changed": True,
                "equipment_changed": False,
            },
            configure_fake=_configure(
                _with_run_settings,
                _wellness(wellness_rows("2026-09-04")),
                _activities(
                    activity_row(
                        "i-author-01", "2026-09-01", minutes=45, distance_m=9000,
                        avg_speed=3.33, hr=152,
                    )
                ),
            ),
            seed_evidence=seed_plan_authoring_evidence,
        ),
        # ---- a fourth week reading its own first week ---------------------------------
        #
        # All three run at day 26, all ask about week one, and all three roll the plan's
        # week forward first. The roll is the point. 03 already reads a cycle back, but
        # its plan was authored for week one and never reviewed, so at day 26 week one's
        # sessions are still the stored week -- their prescriptions are in `plan_state`
        # whatever the cycle record says, and a read that looks like it crossed three
        # weeks did not have to. After a weekly review has rolled the week three times,
        # week one exists only in the commit chain, which is the shape a fourth-week
        # review actually reads.
        Scenario(
            name="13_review_cycle__measurement_reference_in_week_one",
            modes=("review_cycle",),
            purpose=(
                "Day 26 of a plan reviewed weekly, whose measurement reference is week "
                "one's quality run and whose repeat is this week -- both readings in"
            ),
            now=NOW_CYCLE_REVIEW,
            plan=plan_measuring_week_one_quality,
            body={},
            configure_fake=_configure(
                _with_run_settings,
                _wellness(wellness_rows("2026-09-04")),
                _activities(
                    # Same day and sport as the reference session, paired to nothing, so
                    # it attaches as `probable` -- the one tier a human still settles by
                    # reading the activity against what the session asked for.
                    activity_row(
                        "i-reference-01", "2026-08-13", minutes=50, distance_m=9000,
                        avg_speed=3.0, hr=163,
                    ),
                    activity_row(
                        "i-comparison-01", "2026-09-03", minutes=49, distance_m=9000,
                        avg_speed=3.06, hr=157,
                    ),
                ),
            ),
            seed_store=roll_the_week_to_the_measurement_week,
        ),
        Scenario(
            name="14_review_cycle__two_sessions_one_day",
            modes=("review_cycle",),
            purpose=(
                "The same day-26 read of a week-one double day, where one of the two "
                "running sessions took the activity and the other is left with no reading"
            ),
            now=NOW_CYCLE_REVIEW,
            plan=plan_with_two_sessions_on_the_quality_day,
            body={},
            configure_fake=_configure(
                _with_run_settings,
                _wellness(wellness_rows("2026-09-04")),
                _activities(
                    activity_row(
                        "i-double-01", "2026-08-13", minutes=50, distance_m=9000,
                        avg_speed=3.0, hr=163,
                    )
                ),
            ),
            seed_store=roll_the_week_to_the_measurement_week,
        ),
        Scenario(
            name="15_review_cycle__strength_reported_and_not",
            modes=("review_cycle",),
            purpose=(
                "The same day-26 read where week one's lower-body day was reported set "
                "by set and its upper-body day was not, so one lift has a second record "
                "and one has none"
            ),
            now=NOW_CYCLE_REVIEW,
            plan=lambda: strength_alias_baseline(publishable_plan()),
            body={},
            configure_fake=_configure(
                _with_run_settings,
                _wellness(wellness_rows("2026-09-04")),
                _activities(),
            ),
            seed_store=roll_the_week_to_the_measurement_week,
            seed_evidence=seed_strength_alias_evidence,
        ),
        # ---- a quality session family, mid-progression -------------------------------
        Scenario(
            name="16_plan_week__quality_series_concedes_once",
            modes=("plan_week",),
            purpose=(
                "Day 26 of a plan reviewed weekly, whose threshold anchor built a "
                "three-week rising pattern and then, on its fourth exposure, came back "
                "at barely half the prescribed time -- issue #255's question of "
                "whether a coach reads the family or only the last exposure"
            ),
            now=NOW_CYCLE_REVIEW,
            plan=publishable_plan,
            body={},
            configure_fake=_configure(
                _with_run_settings,
                _wellness(wellness_rows("2026-09-04")),
                _activities(
                    activity_row(
                        "i-quality-series-01", "2026-08-13", minutes=50, distance_m=9000,
                        avg_speed=3.0, hr=161,
                    ),
                    activity_row(
                        "i-quality-series-02", "2026-08-20", minutes=56, distance_m=10400,
                        avg_speed=3.10, hr=162,
                    ),
                    activity_row(
                        "i-quality-series-03", "2026-08-27", minutes=58, distance_m=10600,
                        avg_speed=3.05, hr=163,
                    ),
                    activity_row(
                        "i-quality-series-04", "2026-09-03", minutes=32, distance_m=5400,
                        avg_speed=2.81, hr=167,
                    ),
                ),
            ),
            seed_store=roll_the_week_through_a_quality_series_that_concedes_once,
        ),
        # ---- a strength pair the provider cannot tell apart --------------------------
        Scenario(
            name="17_plan_week__strength_pair_same_label",
            modes=("plan_week",),
            purpose=(
                "The Monday after a third week whose two strength days -- a heavy "
                "lower-body session and an easy upper-body one -- arrive with no "
                "body_stress and no cost, carrying only their duration, heart rate, "
                "stated feel and the athlete's own name for each: issue #256's question "
                "of whether a plan_week turn can tell them apart when it decides what "
                "comes next"
            ),
            now=NOW_PLAN_WEEK_FOUR,
            plan=publishable_plan,
            body={},
            configure_fake=_configure(
                _with_run_settings,
                _wellness(wellness_rows("2026-08-31")),
                _activities(
                    {
                        **activity_row(
                            "i-strength-lower-01", "2026-08-24",
                            minutes=55, distance_m=None, avg_speed=None, hr=115,
                            sport="WeightTraining",
                        ),
                        "name": "腿部重訓",
                        "feel": 4,
                    },
                    activity_row(
                        "i-easy-03", "2026-08-25", minutes=54, distance_m=8000,
                        avg_speed=2.469, hr=140,
                    ),
                    activity_row(
                        "i-quality-03", "2026-08-27", minutes=50, distance_m=9000,
                        avg_speed=3.0, hr=161,
                    ),
                    {
                        **activity_row(
                            "i-strength-upper-01", "2026-08-28",
                            minutes=40, distance_m=None, avg_speed=None, hr=100,
                            sport="WeightTraining",
                        ),
                        "name": "上肢維持訓練",
                        "feel": 2,
                    },
                    activity_row(
                        "i-long-03", "2026-08-30", minutes=80, distance_m=12000,
                        avg_speed=2.5, hr=145,
                    ),
                ),
            ),
            seed_store=roll_the_week_to_an_unplanned_strength_pair,
            seed_evidence=seed_unplanned_strength_pair_evidence,
        ),
        # ---- the same session, run on a treadmill -----------------------------------
        Scenario(
            name="18_structured_run__indoor_reps_complete",
            modes=("revisit_today",),
            purpose=(
                "The same multi-step session, run indoors: every prescribed repetition "
                "was completed and every recorded pace is a fifth slow, because a "
                "treadmill's distance is the machine's reading -- issue #252's question "
                "of whether the coach can tell a missed target from an uncomparable "
                "measurement"
            ),
            now=NOW_TODAY,
            plan=lambda: plan_with_execution("run-quality-01", "ev-quality-01"),
            body={},
            configure_fake=_configure(
                _with_run_settings,
                _activities(
                    activity_row(
                        "i-quality-indoor-01", "2026-08-13", minutes=67.5,
                        distance_m=8286, avg_speed=2.046, hr=159,
                        paired_event_id="ev-quality-01", sport="VirtualRun",
                        indoors=True,
                    )
                ),
                _segments("i-quality-indoor-01", *QUALITY_SEGMENTS_INDOOR_ALL_FIVE),
            ),
        ),
        # ---- a cycle review reading a quality session by its repetitions -------------
        #
        # Scenario 13's frame -- day 26, weekly review has rolled the week three times,
        # the cycle's measurement names week one's quality run as its reference -- with
        # one thing added: the provider answers for both readings of that measurement
        # rather than for neither. That is the ordinary case in production, where
        # intervals.icu analyses any structured run, and it is the case no committed read
        # here had: every other late-cycle scenario asks for the older session's
        # breakdown and gets an empty answer, so `segment_execution` reads null and the
        # window it covers never shows.
        #
        # What the two readings are is the point. The reference stopped after three of
        # five repetitions; the comparison ran all five. Their whole-activity averages are
        # 44 minutes at 152 bpm and 60 minutes at 154 bpm, and read as two attempts at one
        # session they say the athlete got slightly worse. Read with the repetitions in
        # hand they say the first attempt was three quarters of a session and the second
        # was the first complete one. Which of those a cycle review can say is issue
        # #290's question, and the answer turns on how far back per-segment execution is
        # carried.
        Scenario(
            name="19_review_cycle__week_one_quality_segments",
            modes=("review_cycle",),
            purpose=(
                "The same day-26 read where both ends of the cycle's measurement came "
                "back with the provider's per-segment breakdown -- week one's quality "
                "run stopped after three of five repetitions, this week's repeat ran "
                "all five -- so the older session's repetitions are stated in the read "
                "or nowhere"
            ),
            now=NOW_CYCLE_REVIEW,
            plan=plan_measuring_week_one_quality,
            body={},
            configure_fake=_configure(
                _with_run_settings,
                _wellness(wellness_rows("2026-09-04")),
                _activities(
                    # Both whole-activity rows are the sum of their own segments, so the
                    # two readings of one session cannot contradict each other.
                    activity_row(
                        "i-reference-01", "2026-08-13", minutes=44, distance_m=6444,
                        avg_speed=2.441, hr=152,
                    ),
                    activity_row(
                        "i-comparison-01", "2026-09-03", minutes=60, distance_m=9082,
                        avg_speed=2.523, hr=154,
                    ),
                ),
                _segments("i-reference-01", *QUALITY_SEGMENTS_THREE_OF_FIVE),
                _segments("i-comparison-01", *QUALITY_SEGMENTS_FIVE_OF_FIVE),
            ),
            seed_store=roll_the_week_to_the_measurement_week,
        ),
        # ---- a cycle that was executed ---------------------------------------------
        # Every other read here is sparse, which is honest about a real account and
        # leaves one question unaskable: what a coach does when the method it is being
        # asked to abandon demonstrably ran. Nothing else supplies that -- the densest
        # committed read holds 19 cycle sessions with 3 completed and 2 attached -- so
        # the case that puts it (issue #217) had nowhere to bind.
        Scenario(
            name="22_plan_cycle__the_cycle_was_executed",
            modes=("plan_cycle",),
            purpose=(
                "Day 26 of a plan reviewed weekly whose sessions were actually trained: "
                "every prescribed run and lift back with an activity, the threshold "
                "series holding its pace across three weeks, and both ends of the "
                "cycle's own measurement in with the repeat faster at a lower heart rate"
            ),
            now=NOW_CYCLE_REVIEW,
            plan=plan_measuring_week_one_quality,
            body={},
            configure_fake=_configure(
                _with_run_settings,
                _wellness(wellness_rows("2026-09-04")),
                _activities(
                    # Week one. The reference reading of the cycle's measurement is the
                    # quality run on 08-13; its repeat on 09-03 is the comparison.
                    activity_row("i-str-01", "2026-08-10", minutes=52, distance_m=0,
                                 avg_speed=0.0, hr=118, sport="WeightTraining"),
                    activity_row("i-easy-01", "2026-08-11", minutes=53, distance_m=8000,
                                 avg_speed=2.52, hr=142),
                    activity_row("i-reference-01", "2026-08-13", minutes=50,
                                 distance_m=9000, avg_speed=3.0, hr=163),
                    activity_row("i-str-02", "2026-08-14", minutes=44, distance_m=0,
                                 avg_speed=0.0, hr=112, sport="WeightTraining"),
                    activity_row("i-long-01", "2026-08-16", minutes=80, distance_m=12000,
                                 avg_speed=2.5, hr=145),
                    # Week two: one repetition more at the same pace.
                    activity_row("i-str-03", "2026-08-17", minutes=52, distance_m=0,
                                 avg_speed=0.0, hr=119, sport="WeightTraining"),
                    activity_row("i-easy-02", "2026-08-18", minutes=53, distance_m=8000,
                                 avg_speed=2.52, hr=141),
                    activity_row("i-quality-02", "2026-08-20", minutes=56,
                                 distance_m=10000, avg_speed=2.98, hr=162),
                    activity_row("i-str-04", "2026-08-21", minutes=45, distance_m=0,
                                 avg_speed=0.0, hr=113, sport="WeightTraining"),
                    activity_row("i-long-02", "2026-08-23", minutes=86, distance_m=13000,
                                 avg_speed=2.52, hr=144),
                    # Week three: the same work, held rather than added to.
                    activity_row("i-str-05", "2026-08-24", minutes=51, distance_m=0,
                                 avg_speed=0.0, hr=118, sport="WeightTraining"),
                    activity_row("i-easy-03", "2026-08-25", minutes=52, distance_m=8000,
                                 avg_speed=2.56, hr=140),
                    activity_row("i-quality-03", "2026-08-27", minutes=55,
                                 distance_m=10000, avg_speed=3.03, hr=161),
                    activity_row("i-str-06", "2026-08-28", minutes=45, distance_m=0,
                                 avg_speed=0.0, hr=112, sport="WeightTraining"),
                    activity_row("i-long-03", "2026-08-30", minutes=92, distance_m=14000,
                                 avg_speed=2.54, hr=143),
                    # Week four: volume down, then the measurement repeated.
                    activity_row("i-easy-04", "2026-09-01", minutes=40, distance_m=6000,
                                 avg_speed=2.5, hr=139),
                    activity_row("i-str-07", "2026-09-02", minutes=50, distance_m=0,
                                 avg_speed=0.0, hr=117, sport="WeightTraining"),
                    activity_row("i-comparison-01", "2026-09-03", minutes=49,
                                 distance_m=9000, avg_speed=3.06, hr=157),
                ),
            ),
            seed_store=roll_the_week_to_the_measurement_week,
        ),
        # ---- what the athlete said -------------------------------------------------
        # Added rather than folded into 01 and 02, which the frozen A/B arms in
        # evals/ab pin: an arm is that commit's answer with one field swapped, so
        # seeding a scenario an arm covers invalidates a measurement that was never
        # about this field. These two are the same reads with the statements added.
        Scenario(
            name="20_revisit_today__one_statement",
            modes=("revisit_today",),
            purpose=(
                "Today's session where the athlete has said one thing about how today "
                "feels, with the recovery trends reading within baseline around it"
            ),
            now=NOW_TODAY,
            plan=publishable_plan,
            body={
                "recovery_signals": recovery_upload("2026-08-13"),
                "available_days": ["mon", "tue", "wed", "thu", "fri"],
            },
            configure_fake=_configure(_with_run_settings, _activities()),
            seed_evidence=seed_today_statement,
        ),
        Scenario(
            name="21_review_week__statements_across_the_fortnight",
            modes=("review_week",),
            purpose=(
                "The same Monday week review, where the athlete has said the same "
                "thing on five days across both calendar weeks and every wellness "
                "trend still reads within baseline"
            ),
            now=NOW_WEEK_REVIEW,
            plan=publishable_plan,
            body={
                "recovery_signals": recovery_upload("2026-08-17"),
                "available_days": ["mon", "tue", "wed", "thu", "fri"],
                "schedule_changed": True,
                "equipment_changed": False,
            },
            configure_fake=_configure(
                _with_run_settings,
                _wellness(wellness_rows("2026-08-17")),
                _activities(
                    activity_row(
                        "i-review-01", "2026-08-11", minutes=30, distance_m=4200,
                        avg_speed=2.33, hr=149,
                    )
                ),
            ),
            seed_evidence=seed_fortnight_of_statements,
        ),
        Scenario(
            name="23_review_week__same_rollup_opposite_order",
            modes=("review_week",),
            purpose=(
                "Two sessions of one lift whose per-load arithmetic is identical and "
                "whose set order is opposite -- the direction lives only in the "
                "session record's ordered sets"
            ),
            now=NOW_TODAY,
            plan=publishable_plan,
            body={},
            configure_fake=_configure(_with_run_settings, _activities()),
            seed_evidence=seed_same_rollup_opposite_order,
        ),
        # ---- recovery reading worse across several days, not one --------------------
        #
        # Every other scenario carrying recovery_signals holds it flat at 55/56 -- a
        # reading that is present without arguing for anything -- which is deliberate
        # for the cases that turn on a coach *not* over-reading a thin or single-day
        # signal. Issue #158's third condition is the opposite pressure: readiness, HRV,
        # sleep and resting heart rate all moving the same way across three days, with
        # no symptom stated anywhere in the turn, is a case no existing scenario's data
        # can support without the given contradicting it.
        Scenario(
            name="24_revisit_today__recovery_declining",
            modes=("revisit_today",),
            purpose=(
                "The evening before the week's threshold anchor, where readiness, HRV, "
                "sleep and resting heart rate have all read worse on each of the last "
                "three days and nothing names a cause"
            ),
            now=NOW_BEFORE_THE_KEY_SESSION,
            plan=publishable_plan,
            body={
                "recovery_signals": declining_recovery_upload("2026-08-12"),
                "available_days": ["mon", "tue", "wed", "thu", "fri"],
            },
            configure_fake=_configure(_with_run_settings, _activities()),
        ),
    ]


# -- running one scenario -----------------------------------------------------------


def _endpoint(url: str) -> str:
    """One provider request trimmed to its endpoint and query.

    The athlete-scoped prefix is dropped so the list reads as endpoints, matching what
    ``GatewayProviderRequestBudgetTests.provider_gets`` in test_gateway.py already
    prints. The query survives, unlike there: the date window a read asks for is part of
    what was read, and a window that quietly narrows is a lost read that an endpoint
    list alone would report as unchanged.
    """
    parsed = urllib.parse.urlsplit(url)
    path = parsed.path
    for prefix in ("/api/v1/athlete/0", "/api/v1"):
        if path.startswith(prefix):
            path = path[len(prefix):] or "/"
            break
    return f"{path}?{parsed.query}" if parsed.query else path


def run_response(
    scenario: Scenario,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None, list[str]]:
    """Drive one ``start_session``: the whole answer, any failure, and what it cost.

    The store is built fresh under ``tempfile.TemporaryDirectory()`` and removed on the
    way out, so this can never read or write the athlete's own state no matter what
    ``GARMIN_COACH_LOOP_HOME`` says.

    The answer is returned whole rather than already projected, because one field of it
    -- the training judgment -- is deliberately kept out of the committed file and still
    has to be assertable.
    """
    plan = scenario.plan() if scenario.plan is not None else None

    with tempfile.TemporaryDirectory(prefix="gcl-scenario-") as tmp:
        state_root = Path(tmp)
        state_dir = store_module.resolve_state_dir(OWNER_ID, state_root=state_root)
        if plan is not None:
            store_module.init_store(state_dir, plan)
        # Order matters: a delivery reservation is written against a plan that is already
        # on disk, and stored athlete evidence is read by the build that follows both.
        if scenario.seed_store is not None:
            assert plan is not None, f"{scenario.name} seeds the store but has no plan"
            scenario.seed_store(state_dir, plan, scenario.now)
        if scenario.seed_evidence is not None:
            scenario.seed_evidence(state_dir)

        fake = FakeIntervals(plan=plan)
        if scenario.configure_fake is not None:
            scenario.configure_fake(fake)
        fetch = scenario.wrap_fetch(fake) if scenario.wrap_fetch is not None else fake

        gateway = CoachGateway(
            GatewayConfig(
                state_root=state_root,
                token_hmac_key=HMAC_KEY,
                intervals_client_id=CLIENT_ID_VALUE,
                intervals_client_secret=CLIENT_SECRET_VALUE,
            ),
            fetch=fetch,
            now=lambda: scenario.now,
        )
        response: dict[str, Any] | None = None
        raised: dict[str, Any] | None = None
        try:
            response = gateway.start_session(OWNER_ID, TOKEN, copy.deepcopy(scenario.body))
        except ContextBuildError as exc:
            # A read that ends the turn is still a read, and which requests it had already
            # spent is exactly what this regression is for. Caught narrowly: only the
            # blocked-build failure is a behaviour a scenario may pin. Anything else
            # escaping here is a broken scenario, and it should stop the run loudly rather
            # than be written into a file as though it were the product's answer.
            raised = {
                "error": type(exc).__name__,
                "message": str(exc),
                "upstream_status": exc.upstream_status,
            }
        requests = [f"{method} {_endpoint(url)}" for method, url in fake.calls]

    return response, raised, requests


def run(scenario: Scenario) -> dict[str, Any]:
    """One scenario, projected into the shape that is committed."""
    return snapshot(scenario, *run_response(scenario))


def snapshot(
    scenario: Scenario,
    response: dict[str, Any] | None,
    raised: dict[str, Any] | None,
    requests: list[str],
) -> dict[str, Any]:
    """The part of one answer that is committed.

    Everything the response carries is kept except ``coaching_guidance``: it is the same
    several thousand characters of training judgment on every one of these answers, its
    size already has a ceiling in ``test_orchestration_prompt.py``, and eighteen identical
    copies here would turn one edit to that text into an eighteen-file diff that hides
    whatever else moved in the same regeneration. The test asserts separately that the
    field is still present and still that text, so dropping it from the file does not drop
    it from the regression.

    Nothing else is trimmed. ``plan_state.current_plan`` in particular stays whole: the
    plan a reconciling read hands back is not the plan it was given -- sessions close and
    the version moves -- so it is an output, not an echo of the fixture.

    ``response`` and ``raised`` are both always present and exactly one is null. A turn
    that ended in a blocked build has no response to compare field by field, and writing
    the failure into the same file is what keeps "this read still fails, here, for this
    reason" a thing a later change has to re-bless rather than something that quietly
    starts or stops happening. No traceback is recorded: line numbers move for reasons
    that have nothing to do with the athlete's answer.
    """
    kept = (
        None
        if response is None
        else {key: value for key, value in response.items() if key != "coaching_guidance"}
    )
    return {
        "scenario": scenario.name,
        "modes": list(scenario.modes),
        "now": scenario.now.isoformat(),
        "provider_requests": requests,
        "response": kept,
        "raised": raised,
    }


# -- the committed snapshots ---------------------------------------------------------


MANIFEST_NAME = "manifest.json"


def snapshot_path(name: str) -> Path:
    return SNAPSHOTS / f"{name}.json"


def _dump(value: Any) -> str:
    # Indented and key-sorted so a regeneration produces a diff a reviewer can read
    # line by line, which is the only reason to commit the data rather than a digest.
    return json.dumps(value, indent=2, ensure_ascii=False, sort_keys=True) + "\n"


def load_snapshot(name: str) -> dict[str, Any]:
    path = snapshot_path(name)
    if not path.is_file():
        raise AssertionError(
            f"no committed snapshot for scenario {name!r} at {path}. "
            f"A new scenario is blessed by running: {REGENERATE_COMMAND}"
        )
    return json.loads(path.read_text(encoding="utf-8"))


def manifest() -> dict[str, Any]:
    return {
        "purpose": (
            "Committed startCoachSession reads: what the coach was handed, and every "
            "provider request it cost, for one fixed instant per scenario"
        ),
        "scope": (
            "Anonymous and synthetic; built only from examples/garmin-coach-loop-28-day "
            "and the fixtures in tests/coach_session_scenarios.py"
        ),
        "private_data": False,
        "regenerate": REGENERATE_COMMAND,
        "scenarios": {scenario.name: scenario.purpose for scenario in scenarios()},
    }


def write_all() -> list[Path]:
    """Rewrite every snapshot and the manifest, and remove any that no longer belong."""
    SNAPSHOTS.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for scenario in scenarios():
        path = snapshot_path(scenario.name)
        path.write_text(_dump(run(scenario)), encoding="utf-8")
        written.append(path)
    manifest_path = SNAPSHOTS / MANIFEST_NAME
    manifest_path.write_text(_dump(manifest()), encoding="utf-8")
    written.append(manifest_path)

    expected = {path.name for path in written}
    for stale in sorted(SNAPSHOTS.glob("*.json")):
        if stale.name not in expected:
            stale.unlink()
            print(f"removed {stale.relative_to(ROOT)} (no scenario declares it)")
    return written


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__ or "")
    parser.add_argument(
        "--write",
        action="store_true",
        help="rewrite the committed snapshots from this checkout's behaviour",
    )
    args = parser.parse_args(argv)
    if not args.write:
        parser.error("nothing to do without --write; the test run reads the snapshots")
    for path in write_all():
        print(f"wrote {path.relative_to(ROOT)}")
    print(
        "\nReview the diff before committing: every line of it is a change in what the "
        "coach is handed."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
