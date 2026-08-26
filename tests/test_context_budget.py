"""A ceiling per CoachContext field, and a stated reason for each one.

AGENTS.md 13 already holds the tool descriptions, the input schemas and the two served
prompts to one finite budget. The context fields are the larger half of what the model
reads and were never held to it: between 2026-08-15 and 2026-08-22, thirteen separate
"let the coach also see X" changes landed, each defensible on its own, none of which
counted the total. On 2026-08-23 one ``startCoachSession`` against the owner's account
returned 77,166 characters -- past the per-result limit of the client it was called
from, which is a size no coaching turn can pay before it says anything (issue #233).

The fixture below is a deliberately heavy athlete, not an average one: a full six-week
provider window at nine sessions a week, a year of imported history, an interval session
with twenty segments, every optional evidence group present. The ceilings are measured
against that. They are tests rather than notes for the same reason the prompt budgets
are: raising one has to be a decision, and a field that wants more space has to say what
it buys and what it replaces.

The ceilings are set with headroom over what the heavy fixture actually produces -- they
bound the shape of a field, not the athlete's training. A field that crosses one has
changed shape, which is the change worth stopping.
"""

from __future__ import annotations

import copy
import datetime as dt
import json
import unittest
from typing import Any

from garmin_coach_loop import context_core
from garmin_coach_loop.context_core import (
    ALL_DAYS,
    DEFAULT_SESSION_MINUTES,
    DEFAULT_TIMEZONE,
    RED_FLAG_FIELDS,
    ContextRequest,
    assemble_context,
    coverage_entry,
)
from garmin_coach_loop.orchestration import training_judgment
from tests.test_orchestration_prompt import MAX_TRAINING_CHARACTERS


NOW = dt.datetime(2026, 1, 8, 12, 0, 0, tzinfo=dt.timezone.utc)
AS_OF_RAW = "2026-01-08T20:00:00+08:00"
AS_OF_DATE = dt.date(2026, 1, 8)


# Per-field ceilings, in characters of compact JSON, each set just above what the heavy
# fixture below actually produces. The number is not the point -- the reason beside it is,
# and a change to either is visible in one diff.
#
# Three of them are windows rather than shapes, and the windows differ because the
# questions differ. Reconciling them into one span was tried and is wrong: they are not
# the same question asked over different lengths.
#
#   recent_actuals      per session, from review_frame.detail_horizon_start. The provider
#                       is still read over six weeks -- matching, the cycle record and
#                       baseline_evidence all run against that -- but the rows reported
#                       start where a review starts reading them. It is the only place an
#                       activity the plan never prescribed appears at all, which is why
#                       it is per session and not a rollup.
#   reported_activities the same shape for sessions no device recorded, over the same
#                       horizon. An import of a year of training drops a row on every
#                       training day; past the horizon that is training_history's
#                       question, at training_history's grain.
#   segment_execution   per rep, two weeks, and only on days the plan prescribed more
#                       than one step on. A run planned as one continuous effort is
#                       completely reported by the average pace and average heart rate
#                       recent_actuals already carries, and reading its auto-laps costs
#                       a provider request per activity for an answer to nothing.
#
# The rest are shapes, and each is one row per thing that happened: a session of the
# cycle, a movement, a baseline claim, a stated weight, a day's recovery reading.
#
# Known duplication left standing, so the next reader does not rediscover it: a lift's
# sets are in both strength_execution (grouped by session) and movement_history (grouped
# by movement, beside what was prescribed and the per-load arithmetic). The two groupings
# answer different questions and both were added for stated reasons; carrying one copy
# and a join key is a separate change with its own case to make.
#
# Whoever makes that case reads issue #238 first. One lift currently appears under two
# exercise keys -- the plan's canonical one and the athlete's own word -- and both
# groupings show the split. Collapsing either group would hide that rather than fix it,
# and the fix belongs upstream of both.
FIELD_BUDGETS: dict[str, int] = {
    # Lowered from 17,000 by issue #240 §1: a row whose settled attachment put its
    # reading on a cycle_sessions record carries only its reconciliation identity
    # (roughly half the characters of a full row). The ceiling is set by the shape
    # that reduces least, not the fixture's current mix: a fresh cycle attaches
    # nothing yet, so every row is full -- measured at this fixture's volume with
    # cycle_sessions empty, that is ~12,700 after §3's inapplicable-null cut -- and
    # the ceiling must not turn that legitimate shape into a red build.
    "recent_actuals": 15_000,
    # Set from the shape that grows most, not from this fixture's mix -- the same rule
    # the recent_actuals line above states, and the one the 8,500 it replaces broke.
    # Every row carries what its session prescribed again (the A/B eval in `evals/ab`
    # measured what dropping it cost the coach), and a row's other half is its attached
    # activity: this fixture attaches 14 of 24 rows and measures 8,211, while an athlete
    # who trained every session attaches all 24 and measures 10,087. The second number
    # is the legitimate one to bound, so 8,500 would have turned a fully-trained cycle
    # red -- and so would 9,700, which is what this line said before somebody rebuilt
    # that shape by hand and got a smaller answer than the builder gives. It is a test
    # now (`test_a_fully_attached_cycle_still_fits_its_ceiling`) rather than a number in
    # a comment, because a number in a comment is exactly what was wrong.
    #
    # What holds "prose did not creep back" is not this line -- a total cannot tell more
    # rows from fatter rows -- but MAX_CYCLE_SESSION_CHARACTERS below.
    "cycle_sessions": 10_500,
    "movement_history": 9_000,
    "reported_activities": 8_000,
    "strength_execution": 7_000,
    "training_history": 6_000,
    "segment_execution": 5_000,
    "baseline_evidence": 3_500,
    "recovery_signals": 3_000,
    "subjective_states": 2_000,
    "current_calendar": 2_000,
    "body_measurements": 1_500,
    # Everything else is a fixed number of rows -- the goal, the frame, the constraints,
    # the baseline, the coverage and freshness tables, the two standing-statement groups.
    # None of them grow with how much the athlete trains, and the test below fails if a
    # field that does arrives without a line here.
}

# The whole context at the heavy fixture's scale. It is below the sum of the ceilings
# above on purpose: the ceilings bound one field each, and this bounds all of them at
# once, which is the case a per-field budget cannot see. A new field is paid for out of
# this, not beside it.
#
# For scale: the owner's live account on 2026-08-23 produced a 55,174-character context
# before this issue's cuts and roughly 33,000 after, against a 62,253-character fixture.
# The fixture is a maximal athlete -- six weeks at ten sessions a week, a year of
# imported history, an interval session of twenty segments, every optional group present
# -- so the gap between the two is headroom, not slack.
MAX_CONTEXT_CHARACTERS = 66_000

# What one row of `cycle_sessions` costs in *keys*: every key the builder can emit
# present at once, with the one provider-supplied free-text field held at a fixed
# representative width. Measured at 495.
#
# The two halves of that sentence are both deliberate. It is the check the per-field
# ceiling cannot be: a field total rises when a cycle holds more sessions, which is not
# growth in what a session costs, and it falls when the athlete trains less, which is
# not a saving. And it bounds the shape rather than the bytes, because `session_label`
# is the athlete's own title for a session and has no width this repository gets to
# choose -- a longer title is not a change anybody made, so measuring it would make this
# number drift for a reason no diff explains. Anything *added* to a row -- a second
# rendering, a fallback description, a cue -- moves it, and has to be argued for in the
# diff that adds it (AGENTS.md 13).
MAX_CYCLE_SESSION_CHARACTERS = 520

# Every key `assemble_context` can put on `cycle_sessions[].activity` at once: the three
# it always writes, `subjective_feel`, the id a row inside the activity-id window keeps,
# the three that appear by sport applicability, and the provider's own label. No real row
# is required to carry all nine -- a strength row has no distance -- but the ceiling is
# set by the shape that carries the most, not by whichever mix a fixture happens to hold.
WIDEST_CYCLE_SESSION_ACTIVITY = {
    "match_confidence": "matched",
    "duration_minutes": 55,
    "average_hr": 150.0,
    # An integer 1-5, which is what the validator accepts; a string here would measure a
    # shape the product cannot emit.
    "subjective_feel": 5,
    "activity_id": "intervals:i00000000000",
    "distance_km": 10.0,
    "average_pace_sec_per_km": 330,
    "elevation_gain_m": 40.0,
    # Provider text, at a plausible width for one. Fixed on purpose -- see above.
    "session_label": "Chest and Triceps Day",
    # Where the run was recorded, which is what says whether the pace above is a
    # measurement or a treadmill's reading.
    "recorded_indoors": True,
}


def _request() -> ContextRequest:
    return ContextRequest(
        as_of_raw=AS_OF_RAW,
        timezone_name=DEFAULT_TIMEZONE,
        available_days=list(ALL_DAYS),
        session_minutes=DEFAULT_SESSION_MINUTES,
        red_flags={field: False for field in RED_FLAG_FIELDS},
        leg_fatigue="unknown",
        soreness="unknown",
        schedule_changed=None,
        equipment_changed=None,
        extra_unknowns=[],
    )


def _window() -> context_core.BuildWindow:
    return context_core.build_window(_request(), NOW)


def _plan() -> dict[str, Any]:
    """A four-week cycle whose current week prescribes nine sessions."""
    sessions: list[dict[str, Any]] = []
    for offset in range(7):
        day = (dt.date(2026, 1, 5) + dt.timedelta(days=offset)).isoformat()
        sessions.append({
            "session_id": f"strength-{day}",
            "sport": "strength",
            "scheduled_date": day,
            "time_window": None,
            "purpose": "維持上肢肌力，週期內不加量",
            "adaptation": "strength",
            "body_stress": "upper",
            "cost": "moderate",
            "priority": "anchor",
            "planned_minutes": 60,
            "hard": False,
            "fallback": {"action": "reduce", "description": "第五組撐不住就做四下收，不降重量"},
            "execution": {
                "publish_supported": True,
                "external_id": f"event-strength-{offset}",
                "delivery_state": "intervals_accepted",
            },
            "match_status": "planned",
            "prescription": "臥推 5x5 65公斤",
            "plan": {
                "kind": "movement_list",
                "movements": [{
                    "exercise": "bench_press", "display_name": "臥推",
                    "sets": 5, "reps": 5, "load_kg": 65.0, "assist_kg": None,
                    "load_basis": "measured_baseline",
                }],
            },
        })
    for offset, structured in ((3, True), (6, False)):
        day = (dt.date(2026, 1, 5) + dt.timedelta(days=offset)).isoformat()
        steps: list[dict[str, Any]] = [{
            "kind": "work", "name": "輕鬆跑",
            "duration": {"kind": "time", "seconds": 2400},
            "target": {"kind": "hr_ceiling", "unit": "bpm", "ceiling_bpm": 140},
        }]
        if structured:
            steps = [
                {"kind": "work", "name": "熱身",
                 "duration": {"kind": "time", "seconds": 900},
                 "target": {"kind": "open"}},
                {"kind": "repeat", "repetitions": 6, "steps": [
                    {"kind": "work", "name": "快跑",
                     "duration": {"kind": "time", "seconds": 180},
                     "target": {"kind": "pace", "unit": "sec_per_km",
                                "low_seconds_per_km": 310, "high_seconds_per_km": 325}},
                    {"kind": "work", "name": "慢跑恢復",
                     "duration": {"kind": "time", "seconds": 180},
                     "target": {"kind": "open"}},
                ]},
                {"kind": "work", "name": "緩和",
                 "duration": {"kind": "time", "seconds": 600},
                 "target": {"kind": "open"}},
            ]
        sessions.append({
            "session_id": f"running-{day}",
            "sport": "running",
            "scheduled_date": day,
            "time_window": "morning",
            "purpose": "累積可控的閾值刺激",
            "adaptation": "threshold" if structured else "aerobic_base",
            "body_stress": "lower",
            "cost": "hard" if structured else "easy",
            "priority": "anchor" if structured else "flexible",
            "planned_minutes": 55 if structured else 40,
            "hard": structured,
            "fallback": {"action": "replace", "description": "改成 30 分鐘輕鬆跑"},
            "execution": {
                "publish_supported": True,
                "external_id": f"event-running-{offset}",
                "delivery_state": "intervals_accepted",
            },
            "match_status": "planned",
            "prescription": "熱身 15分\n6趟：快跑 3分 配速 5:10-5:25/km、慢跑恢復 3分",
            "plan": {"kind": "time_axis", "name": "VO2max 6×3 分鐘", "steps": steps},
        })
    return {
        "schema_version": "1.0",
        "plan_id": "budget-fixture-plan",
        "version": 4,
        "status": "active",
        "goal": {
            "outcome": "改善可重複的 5K 表現，同時維持下肢肌力",
            "measurement_protocol": "在第 0 天與第 28 天於同一條路線、可比條件下重跑 5K",
        },
        "cycle": {
            "start": "2025-12-15",
            "end": "2026-01-11",
            "primary_adaptation": "threshold",
            "maintenance_adaptation": "strength",
            "planned_evidence": ["每個計畫週完成一次可控的閾值錨點"],
            "adjust_conditions": ["連續兩週未達主要刺激"],
            "stop_conditions": ["疼痛、生病、胸痛、暈眩或異常症狀需要人的判斷"],
            "outlook": [],
        },
        "week": {"start": "2026-01-05", "intent": "保住週四的質量課，維持兩次肌力", "sessions": sessions},
        "athlete_baseline": {
            "threshold_pace_sec_per_km": 370,
            "max_hr": 190,
            "easy_hr_ceiling": 140,
            "longest_recent_run_km": 16.5,
            "weekly_volume_km_4wk_avg": 34.2,
            "max_session_minutes": 90,
            "strength_loads": [
                {"exercise": "bench_press", "display_name": "臥推", "load_kg": 65.0,
                 "assist_kg": None, "scheme": "5x5"},
                {"exercise": "split_squat", "display_name": "分腿蹲", "load_kg": 27.2,
                 "assist_kg": None, "scheme": "5x5"},
                {"exercise": "pull_up", "display_name": "引體向上", "load_kg": None,
                 "assist_kg": 33.0, "scheme": "5x5"},
            ],
        },
    }


def _domain() -> context_core.SourceDomain:
    """Six weeks of provider evidence at nine sessions a week, plus one interval
    session's twenty segments -- the only session the plan prescribed reps on."""
    actuals: list[dict[str, Any]] = []
    for offset in range(41, -1, -1):
        day = (AS_OF_DATE - dt.timedelta(days=offset)).isoformat()
        actuals.append({
            "activity_id": f"intervals:i{200000 + offset}",
            "date": day,
            "sport": "strength",
            "paired_event_id": None,
            "planned_session_id": None,
            "match_confidence": "unmatched",
            "adaptation": "strength",
            "body_stress": "full",
            "cost": "moderate",
            "duration_minutes": 62,
            "distance_km": None,
            "average_pace_sec_per_km": None,
            "average_hr": 91,
            "session_label": "肌力訓練",
            "completion": "completed",
            "elevation_gain_m": None,
            "subjective_feel": None,
        })
        if offset % 7 in (0, 3, 5):
            actuals.append({
                "activity_id": f"intervals:i{300000 + offset}",
                "date": day,
                "sport": "running",
                "paired_event_id": None,
                "planned_session_id": None,
                "match_confidence": "unmatched",
                "adaptation": "aerobic_base",
                "body_stress": "lower",
                "cost": "easy",
                "duration_minutes": 42,
                "distance_km": 7.412,
                "average_pace_sec_per_km": 486,
                "average_hr": 138,
                "session_label": "輕鬆跑",
                "completion": "completed",
                "elevation_gain_m": 24.0,
                "subjective_feel": None,
            })
    segments = [{
        "index": index,
        "provider_type": "WORK",
        "distance_m": 1001.5,
        "moving_time_sec": 463,
        "average_pace_sec_per_km": 462,
        "average_hr": 156.0,
        "max_hr": 171.0,
        "min_hr": 129.0,
        "elevation_gain_m": 1.4,
    } for index in range(20)]
    coverage = coverage_entry(7)
    trend = {"status": "within_baseline", "observed_days": 7, "expected_days": 7}
    return context_core.SourceDomain(
        sources=[{
            "source": "intervals-icu-api",
            "mode": "direct_rest_readonly",
            "doctor_status": "passed",
            "observed_at": "2026-01-08T12:00:00+00:00",
            "data_through": AS_OF_DATE.isoformat(),
            "sanitized": True,
        }],
        freshness_activities="fresh",
        freshness_recovery="fresh",
        actuals_window_start=AS_OF_DATE - dt.timedelta(days=41),
        activity_days=frozenset(
            AS_OF_DATE - dt.timedelta(days=offset) for offset in range(42)
        ),
        coverage_sleep=coverage,
        coverage_hrv=coverage,
        coverage_resting_hr=coverage,
        recovery_trends={"sleep": trend, "hrv": trend, "resting_hr": trend},
        recent_actuals=actuals,
        segment_execution={
            "source": "intervals-icu-api",
            "window_start": (AS_OF_DATE - dt.timedelta(days=13)).isoformat(),
            "window_end": AS_OF_DATE.isoformat(),
            "activities": [{
                "activity_id": "intervals:i300008",
                "date": "2026-01-08",
                "sport": "running",
                "recorded_indoors": False,
                "segments": segments,
            }],
        },
        sport_settings_max_hr=190,
        extra_unknowns=[],
    )


def _evidence_groups() -> dict[str, Any]:
    """Every optional group present, each at the size a year-long athlete produces."""
    horizon = context_core.review_horizon_start(_plan(), AS_OF_DATE)
    reported = [{
        "date": (AS_OF_DATE - dt.timedelta(days=offset)).isoformat(),
        "sport": "running",
        "duration_minutes": 40,
        "distance_km": 5.021,
        "subjective_feel": None,
        "note": "測驗後恢復跑 心率壓低",
        "source": "athlete_imported",
        "imported_from": "Garmin via PersonalOS health.db 2025-01 to 2026-01",
        "provider_actual_same_day": True,
    } for offset in range((AS_OF_DATE - horizon).days + 1)]
    history_activities = [{
        "date": (AS_OF_DATE - dt.timedelta(days=offset)).isoformat(),
        "sport": "running" if offset % 3 else "strength",
        "duration_minutes": 45,
        "distance_km": 6.5 if offset % 3 else None,
        "subjective_feel": None,
        "note": None,
        "source": "athlete_imported",
        "imported_from": "Garmin via PersonalOS health.db 2025-01 to 2026-01",
    } for offset in range(365)]
    sets = [{"set": index, "weight_kg": 65.0, "assist_kg": None, "reps": 5, "rpe": None}
            for index in range(1, 6)]
    strength_sessions = [{
        "date": (AS_OF_DATE - dt.timedelta(days=offset)).isoformat(),
        "exercise": "bench_press",
        "category": "upper",
        "sets": copy.deepcopy(sets),
        "notes": ["第五組最後一下卡了一下"],
        "source": "athlete_reported",
    } for offset in range(12)]
    # One movement no baseline names, beside baselines the window holds nothing for
    # (split_squat and pull_up above are never trained in this fixture). This is what
    # makes the unknowns alias-miss line fire (issue #238's second layer), so its cost
    # is measured by this suite instead of assumed bounded.
    strength_sessions.append({
        "date": AS_OF_DATE.isoformat(),
        "exercise": "側平舉",
        "category": "upper",
        "sets": [{"set": 1, "weight_kg": 8.0, "assist_kg": None, "reps": 12, "rpe": None}],
        "notes": [],
        "source": "athlete_reported",
    })
    return {
        "strength_execution": {
            "source": "athlete_reported",
            "window_start": (AS_OF_DATE - dt.timedelta(days=41)).isoformat(),
            "window_end": AS_OF_DATE.isoformat(),
            "sessions": strength_sessions,
        },
        "recovery_signals": {
            "source": "client-uploaded",
            "window_start": (AS_OF_DATE - dt.timedelta(days=6)).isoformat(),
            "window_end": AS_OF_DATE.isoformat(),
            "days": [{
                "date": (AS_OF_DATE - dt.timedelta(days=offset)).isoformat(),
                "readiness_score": 71.0, "readiness_level": "moderate",
                "hrv_status": "balanced", "hrv_7d_avg_ms": 60.0,
                "acute_load": 412.0, "recovery_time_sec": 43200,
                "body_battery_high": 88.0, "body_battery_low": 21.0,
                "avg_stress": 32.0, "sleep_score": 78.0,
                "sleep_duration_sec": 25200, "sleep_history_score": 74.0,
                "hrv_last_night_ms": 62.0, "resting_hr_bpm": 48.0,
            } for offset in range(7)],
        },
        "body_measurements": {
            "source": "athlete_reported",
            "window_start": (AS_OF_DATE - dt.timedelta(days=41)).isoformat(),
            "window_end": AS_OF_DATE.isoformat(),
            "measurements": [{
                "date": (AS_OF_DATE - dt.timedelta(days=offset * 7)).isoformat(),
                "weight_kg": 72.5, "body_fat_pct": 18.0,
                "source": "athlete_reported",
            } for offset in range(6)],
        },
        "reported_activities": {
            "source": "athlete_reported",
            "window_start": horizon.isoformat(),
            "window_end": AS_OF_DATE.isoformat(),
            "activities": reported,
        },
        "subjective_states": {
            "source": "athlete_reported",
            "window_start": (AS_OF_DATE - dt.timedelta(days=13)).isoformat(),
            "window_end": AS_OF_DATE.isoformat(),
            "states": [{
                "date": (AS_OF_DATE - dt.timedelta(days=offset)).isoformat(),
                "note": "腿還是很沉，睡得不錯但起床沒什麼精神",
                "recorded_at": "2026-01-08T12:00:00+00:00",
            } for offset in range(14)],
        },
        "long_term_goals": {
            "source": "athlete_reported",
            "goals": [{"metric": "半程馬拉松完賽時間", "target": "1:45",
                       "target_date": "2027-03-01", "note": None,
                       "recorded_at": "2026-01-08T12:00:00+00:00"}],
        },
        "training_preferences": {
            "source": "athlete_reported",
            "preferences": [{"topic": "每週訓練頻率",
                             "statement": "一週練五次肌力，跑步排在早上",
                             "recorded_at": "2026-01-08T12:00:00+00:00"}],
        },
        "training_history_activities": history_activities,
        "training_history_strength_reports": strength_sessions,
    }


def _heavy_context() -> dict[str, Any]:
    plan = _plan()
    # Raw store sessions, the shape store.cycle_sessions returns -- assemble_context
    # projects them. Three earlier weeks of the same nine-session week.
    cycle_sessions: list[dict[str, Any]] = []
    for offset in range(1, 25):
        day = (AS_OF_DATE - dt.timedelta(days=offset)).isoformat()
        template = copy.deepcopy(
            plan["week"]["sessions"][7 if offset % 3 else 0]
        )
        template.update(
            session_id=f"cycle-{offset}",
            scheduled_date=day,
            match_status="completed",
            execution={
                "publish_supported": True,
                "external_id": f"event-cycle-{offset}",
                "delivery_state": "intervals_accepted",
            },
        )
        cycle_sessions.append(template)
    report = assemble_context(
        _request(), plan, _window(), _domain(),
        cycle_sessions=cycle_sessions,
        **_evidence_groups(),
    )
    assert report["status"] == "passed", report
    return report["context"]


def _size(value: Any) -> int:
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")))


class ContextBudgetTests(unittest.TestCase):
    """One ceiling per field, and the total they have to fit inside together."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.context = _heavy_context()

    def test_every_budgeted_field_fits_its_ceiling(self):
        over = {
            field: (_size(self.context.get(field)), ceiling)
            for field, ceiling in FIELD_BUDGETS.items()
            if _size(self.context.get(field)) > ceiling
        }
        self.assertEqual(
            {}, over,
            "a context field is over its budget (field: actual, ceiling). Raising a "
            "ceiling is a decision: say what the extra buys and what it replaces "
            "(AGENTS.md 13, issue #233)",
        )

    def test_no_single_cycle_row_grows_past_what_a_row_costs(self):
        """Per row, with every key the builder can emit present at once.

        The fixture's own rows are not that shape -- ten of its twenty-four attach
        nothing, and a strength row has no distance -- so the check builds it, which is
        the same rule the field ceilings above follow: bound the shape that carries the
        most, not the mix a fixture happens to hold.
        """
        widest = max(
            _size(
                {
                    **row,
                    "activity": WIDEST_CYCLE_SESSION_ACTIVITY,
                    "activity_evidence": "attached",
                }
            )
            for row in self.context["cycle_sessions"]
        )
        self.assertLessEqual(
            widest, MAX_CYCLE_SESSION_CHARACTERS,
            "one cycle_sessions row now costs more than the budget for a row; whatever "
            "was added to it is paid for by every session of every later turn",
        )

    def test_the_widest_row_shape_is_wider_than_any_row_the_fixture_holds(self):
        """The check above is only worth running while its synthetic shape is the
        larger one. An earlier version of it omitted `session_label` and measured a
        row narrower than one the same fixture already carried, which made it pass
        while claiming to bound something it did not reach.
        """
        widest = max(
            _size(
                {
                    **row,
                    "activity": WIDEST_CYCLE_SESSION_ACTIVITY,
                    "activity_evidence": "attached",
                }
            )
            for row in self.context["cycle_sessions"]
        )
        emitted = max(_size(row) for row in self.context["cycle_sessions"])
        self.assertGreater(
            widest, emitted,
            "the synthetic widest row is narrower than a row the builder emitted; it is "
            "missing a key the builder can write",
        )

    def test_a_fully_attached_cycle_still_fits_its_ceiling(self):
        """The shape the `cycle_sessions` ceiling is set from, built rather than claimed.

        The fixture is an athlete who trained ten of its twenty-four prescribed
        sessions. One who trained all of them attaches an activity to every row, and
        that is the shape the ceiling has to admit -- a build that turns a
        fully-trained cycle red is a build that punishes training.

        Each unattached row is given a real activity from the same fixture, chosen by
        which side of the activity-id window the row sits on, so the reconstruction is
        the builder's own output rather than a hand-written approximation. The last
        attempt at this number was hand-written and came out 639 characters low.
        """
        rows = self.context["cycle_sessions"]
        inside = next(
            row["activity"]
            for row in rows
            if row["activity"] and "activity_id" in row["activity"]
        )
        outside = next(
            row["activity"]
            for row in rows
            if row["activity"] and "activity_id" not in row["activity"]
        )
        monday = max(row["week_start"] for row in rows)
        attached = [
            row
            if row["activity"] is not None
            else {
                **row,
                "activity": inside if row["week_start"] >= monday else outside,
                "activity_evidence": "attached",
            }
            for row in rows
        ]
        self.assertLessEqual(
            _size(attached), FIELD_BUDGETS["cycle_sessions"],
            "a cycle whose every session was trained is over the cycle_sessions "
            "ceiling; the ceiling is set from the fixture's mix instead of from the "
            "shape that grows most",
        )
        self.assertLessEqual(
            _size({**self.context, "cycle_sessions": attached}),
            MAX_CONTEXT_CHARACTERS,
            "the same fully-trained cycle is over the whole-context budget",
        )

    def test_the_widest_row_shape_carries_every_key_the_builder_can_emit(self):
        """Named against the builder rather than trusted, so a new activity field
        fails here instead of quietly leaving the row ceiling measuring less."""
        emitted: set[str] = set()
        for row in self.context["cycle_sessions"]:
            if isinstance(row.get("activity"), dict):
                emitted |= set(row["activity"])
        self.assertEqual(
            set(), emitted - set(WIDEST_CYCLE_SESSION_ACTIVITY),
            "the builder emits an activity key the widest-row shape does not carry",
        )

    def test_the_whole_context_fits_one_client_result(self):
        """The property the per-field ceilings exist to hold.

        Every field inside its own budget still adds up to a response nobody can read
        if the number of fields keeps growing, so the total is held separately -- a new
        field is paid for out of this, not beside it.
        """
        self.assertLessEqual(
            _size(self.context), MAX_CONTEXT_CHARACTERS,
            "the whole CoachContext is over budget; a new field costs an old one",
        )

    def test_the_total_stays_tied_to_the_budgets_that_add_up_to_it(self):
        """The total cannot drift without a budget line moving in the same diff.

        Without this, MAX_CONTEXT_CHARACTERS and FIELD_BUDGETS could be raised
        independently until neither says anything about the other. The allowance is for
        the fixed-size fields nobody budgets individually.
        """
        budgeted = sum(_size(self.context.get(field)) for field in FIELD_BUDGETS)
        self.assertLessEqual(
            _size(self.context) - budgeted, 4_000,
            "the fields with no budget line now exceed the allowance for fixed-size "
            "rows; whichever one grew belongs in FIELD_BUDGETS",
        )

    def test_every_growing_field_is_budgeted(self):
        """A field nobody wrote a ceiling for is how the last thirteen got through.

        Anything large enough to matter has to appear in FIELD_BUDGETS with a reason
        beside it. This fails when a new field arrives unbudgeted, which is the moment
        to write down what it buys rather than the moment it is already too late.
        """
        unbudgeted = sorted(
            field for field, value in self.context.items()
            if field not in FIELD_BUDGETS and _size(value) > 1_000
        )
        self.assertEqual(
            [], unbudgeted,
            "these context fields exceed 1 KB with no stated budget; add each to "
            "FIELD_BUDGETS with the reason it is read every turn",
        )

    def test_the_training_judgment_is_budgeted_somewhere_else_and_stays_there(self):
        """Why ``coaching_guidance`` is not in the numbers above.

        It rides on every ``startCoachSession`` response, identical every time, which
        makes moving it to the served ``instructions`` look free. It is not: MCP
        prompts are user-controlled by specification, and the instructions field is a
        MAY that claude.ai does not honour -- which is why the text moved *into* the
        response in the first place. The response body is the one channel a client
        cannot decline, so this stays a placement decision that was already made.

        What holds its size is its own ceiling in test_orchestration_prompt.py, and
        this asserts only that the ceiling is real and that this file does not quietly
        keep a second copy of the number.
        """
        self.assertLessEqual(len(training_judgment()), MAX_TRAINING_CHARACTERS)
        self.assertNotIn("coaching_guidance", self.context)


if __name__ == "__main__":
    unittest.main()
