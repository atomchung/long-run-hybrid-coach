"""Eighteen fixed ``startCoachSession`` reads, and the command that re-blesses them.

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

from tests.test_gateway import FakeIntervals, RUN_SPORT_SETTINGS, publishable_plan


ROOT = Path(__file__).resolve().parents[1]
SNAPSHOTS = Path(__file__).resolve().parent / "fixtures" / "coach_session_scenarios"

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
) -> dict[str, Any]:
    """One row in the shape intervals.icu answers ``/activities`` with."""
    row: dict[str, Any] = {
        "id": activity_id,
        "type": sport,
        "start_date_local": f"{date}T07:00:00",
        "moving_time": minutes * 60,
        "distance": distance_m,
        "average_speed": avg_speed,
    }
    if hr is not None:
        row["average_heartrate"] = hr
    if paired_event_id is not None:
        row["paired_event_id"] = paired_event_id
    return row


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

    def wrapped(request: urllib.request.Request) -> bytes:
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


def seed_strength_alias_evidence(state_dir: Path) -> None:
    """One lift reported twice: once under the plan's key, once in the athlete's words.

    The example plan prescribes ``back squat`` and displays it as 深蹲. An athlete who
    says 深蹲 is naming the same lift, and today the product files it as a second
    movement -- so ``strength_execution`` and ``movement_history`` both show the split.
    This scenario pins that behaviour rather than correcting it: the split is a known,
    open defect in how movements are keyed, and a snapshot that quietly hid it would
    make the fix harder to see landing, not easier.
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


def _wellness(rows: list[dict[str, Any]]) -> Callable[[FakeIntervals], None]:
    def apply(fake: FakeIntervals) -> None:
        fake.wellness = copy.deepcopy(rows)

    return apply


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
                "The multi-step session pairs with its own activity, so the per-segment "
                "read fires and the plan moves in the same turn"
            ),
            now=NOW_TODAY,
            plan=lambda: plan_with_execution("run-quality-01", "ev-quality-01"),
            body={},
            configure_fake=_configure(
                _with_run_settings,
                _activities(
                    activity_row(
                        "i-quality-01", "2026-08-13", minutes=50, distance_m=10000,
                        avg_speed=3.33, hr=160, paired_event_id="ev-quality-01",
                    )
                ),
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
        # The paired scenario to "10", and the pair is the point. The same outage on an
        # account that has a plan ends the turn -- the build refuses rather than reporting
        # recovery as unknown -- and on an account that has none it costs nothing at all.
        # Whether that asymmetry is right is a live question against AGENTS.md 3 and not
        # one a snapshot settles; what a snapshot does is stop it changing by accident, in
        # either direction, on either branch.
        Scenario(
            name="06_recovery_read_fails",
            modes=("revisit_today",),
            purpose=(
                "The wellness endpoint answers 500 on an account that has a plan: the "
                "build refuses after one retry, and the turn ends with no context"
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
                "One lift reported under the plan's key and again in the athlete's own "
                "word, which today reads back as two movements"
            ),
            now=NOW_TODAY,
            plan=publishable_plan,
            body={},
            configure_fake=_configure(_with_run_settings, _activities()),
            seed_evidence=seed_strength_alias_evidence,
        ),
        Scenario(
            name="08_strength_alias__reconcile",
            modes=("review_week",),
            purpose="The same split, on a read that also reconciles and rebuilds",
            now=NOW_TODAY,
            plan=lambda: plan_with_execution("run-easy-01", "ev-easy-01-strength"),
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
