"""Regression tests for the plan preview.

Every assertion here answers one question: does the page claim anything the store
does not carry? The preview is the athlete's truth surface, so a wrong delivery
state or an invented forecast is a product defect, not a cosmetic one.
"""

from __future__ import annotations

import copy
import importlib.util
import json
import re
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

from garmin_coach_loop.store import default_state_dir


ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "examples" / "garmin-coach-loop-28-day"
CONTRACTS = ROOT / "contracts"


def _load_renderer():
    """Import the renderer from scripts/, which is not an installed package."""
    spec = importlib.util.spec_from_file_location(
        "render_plan_preview", ROOT / "scripts" / "render_plan_preview.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


preview = _load_renderer()


def load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def contract_delivery_states() -> list[str]:
    schema = load(CONTRACTS / "plan-state.schema.json")
    session = schema["$defs"]["session"]["properties"]
    return session["execution"]["properties"]["delivery_state"]["enum"]


def session_with(**overrides) -> dict:
    session = {
        "session_id": "run-01",
        "sport": "running",
        "adaptation": "threshold",
        "execution": {
            "publish_supported": True,
            "external_id": None,
            "delivery_state": "not_published",
        },
    }
    session.update(overrides)
    return session


class DeliveryStateTests(unittest.TestCase):
    """`intervals_accepted` rendered as 已在錶上 was the P0: it told the athlete a
    workout had reached the watch when only Intervals had accepted it."""

    def chip(self, state: str) -> str:
        return preview.delivery_chip(
            session_with(
                execution={
                    "publish_supported": True,
                    "external_id": "e1",
                    "delivery_state": state,
                }
            )
        )

    def test_every_contract_delivery_state_has_its_own_label(self):
        states = contract_delivery_states()
        labels = {state: self.chip(state) for state in states}
        self.assertEqual(len(states), len(set(labels.values())))
        for state, chip in labels.items():
            self.assertNotIn(state, chip, f"{state} fell through to the raw-value chip")

    def test_no_state_claims_the_watch_or_garmin(self):
        # The ladder stops at intervals_accepted, so nothing on this page may imply the
        # workout got past Intervals. Those two hops are the athlete's.
        for state in contract_delivery_states():
            chip = self.chip(state)
            self.assertNotIn("錶", chip, f"{state} implies the workout reached the device")
            self.assertNotIn("Garmin", chip, f"{state} implies Garmin received it")
            self.assertNotIn("Connect", chip, f"{state} implies Garmin Connect received it")

    def test_the_terminal_state_names_where_the_workout_actually_is(self):
        chip = self.chip("intervals_accepted")
        self.assertIn("Intervals", chip)
        self.assertIn("chip-ok", chip)  # a finish line, not something in transit

    def test_unknown_delivery_state_degrades_instead_of_guessing(self):
        chip = self.chip("teleported_to_the_watch")
        self.assertIn("chip-muted", chip)
        self.assertNotIn("已在錶上", chip)

    def test_a_text_prescription_never_carries_a_delivery_state(self):
        chip = preview.delivery_chip(
            session_with(
                execution={
                    "publish_supported": False,
                    "external_id": None,
                    "delivery_state": "not_published",
                }
            )
        )
        self.assertIn("文字處方", chip)


class ContractVocabularyTests(unittest.TestCase):
    """View-local vocabulary outside the contract is a drift path: the page would be
    naming adaptations and sports the plan cannot store."""

    def test_the_zone_map_holds_only_contract_adaptations(self):
        schema = load(CONTRACTS / "plan-state.schema.json")
        self.assertEqual(
            set(schema["$defs"]["adaptation"]["enum"]),
            set(preview.ADAPTATION_ZONE),
        )

    def test_the_sport_labels_hold_only_contract_sports(self):
        schema = load(CONTRACTS / "plan-state.schema.json")
        self.assertEqual(set(schema["$defs"]["sport"]["enum"]), set(preview.SPORT_LABEL))

    def test_an_off_contract_adaptation_is_not_coloured_as_recovery(self):
        # Falling back to z1 read as "this is a recovery session", which is a
        # training claim the plan never made.
        self.assertEqual("zx", preview.zone_for(session_with(adaptation="tempo")))
        self.assertEqual("z4", preview.zone_for(session_with(adaptation="threshold")))


class ReadabilityLayoutTests(unittest.TestCase):
    """The action view leads and the loudest mark belongs to what actually happened.

    These lock the #25/#17 readability decisions: weekly menu before the change card,
    today anchored, completed rows fully opaque with the row's single saturated chip,
    one date per calendar day. The structure bar's own content is covered by
    StructureBarTests below; the two `結構未...` gap chips these once locked were
    retired by #118 -- a session's own plan, not delivery, now decides whether the bar
    has anything to draw, so there is no longer a "missing" state to announce.
    """

    def setUp(self):
        self.plan = load(EXAMPLE / "plan-state-v2-day-4.json")
        self.event = load(EXAMPLE / "decision-event-day-4.json")

    def render(self, today: date = date(2026, 8, 13)) -> str:
        return preview.render_page(self.plan, self.event, {}, today)

    def test_weekly_menu_precedes_the_change_card(self):
        html = self.render()
        self.assertLess(html.index("本週安排"), html.index("這一輪改了什麼"))

    def test_first_screen_leads_with_goal_actions_week_and_visible_change_reasons(self):
        html = self.render()
        focus_start = html.index('<section class="card action-focus">')
        week_start = html.index("本週安排")
        focus = html[focus_start:week_start]
        self.assertIn(self.plan["goal"]["outcome"], focus)
        self.assertIn(self.plan["cycle"]["primary_adaptation"], focus)
        self.assertIn(self.plan["cycle"]["maintenance_adaptation"], focus)
        self.assertIn(self.plan["week"]["intent"], focus)
        self.assertIn(self.event["change"]["summary"], focus)
        self.assertIn("<h3>交付</h3>", focus)
        for evidence in self.event["evidence"][:3]:
            self.assertIn(evidence["observation"], focus)
        self.assertNotIn("<details", focus)

    def test_the_weeks_intent_is_stated_once(self):
        # It headlined the action card and then repeated verbatim as the week grid's
        # subtitle. On a real plan that intent is a paragraph, so the page asked the
        # athlete to read the same wall of text twice before reaching the sessions.
        html = self.render()
        self.assertEqual(1, html.count(self.plan["week"]["intent"]))

    def test_first_screen_names_the_one_confirmation_for_publishable_runs(self):
        plan = copy.deepcopy(self.plan)
        session = next(
            item for item in plan["week"]["sessions"]
            if item["sport"] == "running" and item["match_status"] == "planned"
        )
        session["execution"]["publish_supported"] = True
        html = preview.render_page(plan, self.event, {}, date(2026, 8, 13))
        focus = html[html.index('<section class="card action-focus">'):html.index("本週安排")]
        self.assertIn("待精確 preview 確認", focus)

    def test_default_state_dir_uses_the_store_owner(self):
        self.assertEqual(default_state_dir(), preview.DEFAULT_STATE_DIR)

    def test_load_store_skips_deterministic_events_and_finds_latest_coaching_change(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp)
            commits = state_dir / "commits"
            commits.mkdir()
            coaching = copy.deepcopy(self.event)
            reconciliation = copy.deepcopy(self.event)
            reconciliation["reason_codes"] = ["planned_actual_reconciled"]
            delivery = copy.deepcopy(self.event)
            delivery["reason_codes"] = ["delivery_verified"]
            for sequence, slug, event in (
                (2, "coaching", coaching),
                (3, "reconcile", reconciliation),
                (4, "delivery", delivery),
            ):
                commit = commits / f"{sequence:08d}-{slug}"
                commit.mkdir()
                (commit / "event.json").write_text(json.dumps(event), encoding="utf-8")
            (state_dir / "store.json").write_text(
                json.dumps({"current_sequence": 4}), encoding="utf-8"
            )
            with mock.patch.object(
                preview, "status_store", return_value={"current_plan": self.plan}
            ):
                plan, event = preview.load_store(state_dir)
            self.assertEqual(self.plan, plan)
            self.assertEqual(coaching, event)

    def test_change_card_is_folded_by_default(self):
        html = self.render()
        # The opening tag must not carry `open`, and the title must live inside.
        self.assertIn('<details class="card accent changelog">', html)
        self.assertNotIn('<details class="card accent changelog" open', html)
        details_start = html.index('<details class="card accent changelog">')
        self.assertIn("這一輪改了什麼", html[details_start:])

    def test_changelog_title_is_phrasing_content_with_aria_heading(self):
        # summary's content model is phrasing content: no h2 inside it; the heading
        # semantics travel via ARIA instead.
        html = self.render()
        self.assertNotIn("<summary><h2>", html)
        self.assertIn('role="heading" aria-level="2"', html)

    def test_today_row_is_anchored(self):
        session_date = self.plan["week"]["sessions"][0]["scheduled_date"]
        html = self.render(today=date.fromisoformat(session_date))
        self.assertIn('<span class="today-tag">今天</span>', html)
        self.assertIn('class="srow today"', html)

    def test_a_day_off_page_has_no_today_anchor(self):
        html = self.render(today=date(2030, 1, 1))
        self.assertNotIn('<span class="today-tag">', html)

    def test_rest_day_carries_no_chip_at_all(self):
        session = _completed_running_session()
        session.update({"sport": "rest", "match_status": "planned", "adaptation": "recovery"})
        session["execution"]["publish_supported"] = False
        row = preview.render_session_row(session, {}, 370, today=None)
        self.assertNotIn("chip", row)

    def test_completed_row_is_fully_opaque_and_carries_only_the_done_chip(self):
        row = preview.render_session_row(
            _completed_running_session(), {}, 370, today=None
        )
        self.assertNotIn("missed", row)
        self.assertIn("chip-done", row)
        # Delivery chips stay silent once the session happened.
        self.assertNotIn("chip-ok", row)

    def test_missed_row_gets_the_faded_treatment(self):
        session = _completed_running_session()
        session["match_status"] = "missed"
        row = preview.render_session_row(session, {}, 370, today=None)
        self.assertIn("srow missed", row)
        self.assertNotIn("chip-done", row)

    def test_an_unpublished_sessions_structure_bar_still_renders(self):
        # The deleted parser could only draw from workout text Intervals had already
        # echoed back, so an unpublished session rendered no structure at all (and a
        # quiet "結構未推送" chip said so). The bar now reads the session's own plan,
        # which the store already validated whether or not delivery has happened yet
        # (#118), so a not_published time_axis session draws its bar like any other.
        session = _completed_running_session()
        session.update({"match_status": "planned", "plan": _five_by_1km_plan()})
        session["execution"]["delivery_state"] = "not_published"
        session["execution"]["external_id"] = None
        row = preview.render_session_row(session, {}, 370, today=None)
        self.assertIn('class="sbar"', row)
        self.assertNotIn("結構未", row)

    def test_an_accepted_sessions_structure_bar_does_not_depend_on_being_handed_the_workout_text(self):
        # Store says Intervals accepted it; the old page could still show no bar here
        # if it was not separately handed the workout text, and called that a "結構未
        # 載入" data gap. The plan makes that gap impossible: it is store content, not
        # something a caller has to remember to pass in alongside delivery state.
        session = _completed_running_session()
        session.update({"match_status": "planned", "plan": _five_by_1km_plan()})
        row = preview.render_session_row(session, {}, 370, today=None)
        self.assertIn('class="sbar"', row)
        self.assertNotIn("結構未", row)

    def test_text_prescription_never_reports_a_structure_gap(self):
        # publish_supported=False means the prescription is the delivery; there is no
        # watch structure to be missing.
        session = _completed_running_session()
        session["match_status"] = "planned"
        session["execution"] = {
            "publish_supported": False,
            "external_id": None,
            "delivery_state": "not_published",
        }
        row = preview.render_session_row(session, {}, 370, today=None)
        self.assertIn("文字處方", row)
        self.assertNotIn("結構未", row)

    def test_delivered_strength_never_reports_a_structure_gap(self):
        # Strength reaches the calendar as text, so there is no structure to be missing.
        # Delivering it still forces publish_supported=true (validation requires it after
        # Intervals acceptance), which read alone announced a gap on every strength
        # session the product had actually delivered.
        session = _completed_running_session()
        session.update(
            {"sport": "strength", "adaptation": "strength", "match_status": "planned"}
        )
        row = preview.render_session_row(session, {}, 370, today=None)
        self.assertIn("chip-ok", row)  # the delivery chip still speaks
        self.assertNotIn("結構未", row)

    def test_a_strength_sessions_movement_list_is_never_drawn_as_a_structure_bar(self):
        # Dispatch is on the plan's own `kind`, not on sport: a movement_list plan
        # already reaches the athlete through the prescription paragraph, and the
        # proportional bar is a time_axis-only visual language a set/rep/load scheme
        # must never be forced into (archived issue #118).
        session = _completed_running_session()
        session.update(
            {
                "sport": "strength",
                "adaptation": "strength",
                "match_status": "planned",
                "plan": {
                    "kind": "movement_list",
                    "movements": [
                        {
                            "exercise": "back_squat",
                            "display_name": "分腿蹲",
                            "sets": 5,
                            "reps": 5,
                            "load_kg": 60,
                            "assist_kg": None,
                            "load_basis": "measured_baseline",
                        }
                    ],
                },
            }
        )
        row = preview.render_session_row(session, {}, 370, today=None)
        self.assertNotIn("sbar", row)

    def test_rest_day_announces_neither_structure_nor_delivery_gap(self):
        session = _completed_running_session()
        session.update({"sport": "rest", "match_status": "planned", "adaptation": "recovery"})
        session["execution"]["publish_supported"] = False
        row = preview.render_session_row(session, {}, 370, today=None)
        self.assertNotIn("結構未", row)
        self.assertNotIn("sbar", row)

    def test_completed_rest_day_still_carries_no_chip(self):
        session = _completed_running_session()
        session.update({"sport": "rest", "adaptation": "recovery"})
        session["execution"]["publish_supported"] = False
        row = preview.render_session_row(session, {}, 370, today=None)
        self.assertNotIn("chip", row)

    def test_completed_row_carries_exactly_one_chip(self):
        row = preview.render_session_row(_completed_running_session(), {}, 370, today=None)
        self.assertEqual(1, row.count('class="chip '))
        self.assertIn("chip-done", row)

    def test_missed_today_row_keeps_both_marks(self):
        # A missed session that is also today stays faded and anchored at once --
        # locked so a future "cleanup" does not silently pick one over the other.
        session = _completed_running_session()
        session["match_status"] = "missed"
        row = preview.render_session_row(
            session, {}, 370, today=date.fromisoformat(session["scheduled_date"])
        )
        self.assertIn('class="srow missed today"', row)
        self.assertIn("today-tag", row)

    def test_one_calendar_day_prints_its_date_once(self):
        plan = copy.deepcopy(self.plan)
        first = plan["week"]["sessions"][0]
        twin = copy.deepcopy(first)
        twin["session_id"] = "twin-01"
        plan["week"]["sessions"].append(twin)
        html = preview.render_page(plan, self.event, {}, date(2026, 8, 13))
        day = date.fromisoformat(first["scheduled_date"])
        self.assertEqual(1, html.count(f"<b>{day.month}/{day.day}</b>"))

    def test_dates_reprint_across_days_even_from_unsorted_input(self):
        # render_page sorts sessions itself, so adjacency never depends on input
        # order: a same-day twin appended after a later-day session still collapses,
        # and each distinct day prints its own date.
        plan = copy.deepcopy(self.plan)
        sessions = plan["week"]["sessions"]
        first = sessions[0]
        later = next(
            s for s in sessions if s["scheduled_date"] != first["scheduled_date"]
        )
        twin = copy.deepcopy(first)
        twin["session_id"] = "twin-unsorted-01"
        sessions.append(twin)  # unsorted: same-day twin sits after later days
        html = preview.render_page(plan, self.event, {}, date(2026, 8, 13))
        first_day = date.fromisoformat(first["scheduled_date"])
        later_day = date.fromisoformat(later["scheduled_date"])
        self.assertEqual(1, html.count(f"<b>{first_day.month}/{first_day.day}</b>"))
        self.assertEqual(1, html.count(f"<b>{later_day.month}/{later_day.day}</b>"))

    def test_metrics_column_width_is_pinned(self):
        self.assertIn("min-width:52px", self.render())

    def test_zx_has_a_dark_mode_value(self):
        html = self.render()
        dark_block = html[html.index("prefers-color-scheme: dark"):]
        # The dark-mode :root override must restate --zx; its variables all sit in the
        # first few hundred characters of the media block.
        self.assertIn("--zx:", dark_block[:600])


def _completed_running_session() -> dict:
    return {
        "session_id": "run-done-01",
        "sport": "running",
        "scheduled_date": "2026-08-12",
        "purpose": "test",
        "adaptation": "threshold",
        "planned_minutes": 40,
        "priority": "flexible",
        "hard": False,
        "match_status": "completed",
        "execution": {
            "publish_supported": True,
            "external_id": "128000000",
            "delivery_state": "intervals_accepted",
        },
    }


def _five_by_1km_plan() -> dict:
    """Issue #118's own acceptance scenario: warm-up, 5x(1km work + recovery), cool-down."""
    return {
        "kind": "time_axis",
        "name": "5x1km threshold",
        "steps": [
            {
                "kind": "work",
                "name": "熱身",
                "duration": {"kind": "time", "seconds": 600},
                "target": {"kind": "open"},
            },
            {
                "kind": "repeat",
                "repetitions": 5,
                "steps": [
                    {
                        "kind": "work",
                        "name": "門檻配速",
                        "duration": {"kind": "distance", "meters": 1000},
                        "target": {
                            "kind": "pace",
                            "unit": "sec_per_km",
                            "low_seconds_per_km": 200,
                            "high_seconds_per_km": 210,
                        },
                    },
                    {
                        "kind": "work",
                        "name": "恢復",
                        "duration": {"kind": "time", "seconds": 90},
                        "target": {"kind": "open"},
                    },
                ],
            },
            {
                "kind": "work",
                "name": "緩和",
                "duration": {"kind": "time", "seconds": 600},
                "target": {"kind": "open"},
            },
        ],
    }


class StructureBarTests(unittest.TestCase):
    """The bar is read straight from a session's own `time_axis` plan (archived issue #118).

    The deleted text parser produced three defects: a distance step silently
    vanishing, a repeat's count leaking into the very next top-level step, and a
    duration-like token in a step *name* being read as the step's duration. These
    tests pin the fix at the level of the plan's own discriminated union rather than
    at any rendered text, which is exactly what let the defects go unnoticed.
    """

    def bar(self, plan: dict, threshold_sec: int | None = 230) -> str:
        return preview.render_structure_bar(session_with(plan=plan), threshold_sec)

    def test_a_distance_work_step_renders_instead_of_vanishing(self):
        # Defect 1: the deleted `_STEP` regex only matched a trailing `m`/`s` (minute/
        # second) unit, so a `1km` step never matched and silently dropped out.
        bar = self.bar({
            "kind": "time_axis",
            "name": "distance interval",
            "steps": [{
                "kind": "work",
                "name": "1km 快跑",
                "duration": {"kind": "distance", "meters": 1000},
                "target": {
                    "kind": "pace", "unit": "sec_per_km",
                    "low_seconds_per_km": 200, "high_seconds_per_km": 210,
                },
            }],
        })
        self.assertEqual(1, bar.count("<i "))
        self.assertIn('title="1km 快跑"', bar)

    def test_5x1km_renders_five_work_segments_five_recoveries_and_one_cooldown(self):
        # The issue's own acceptance scenario.
        bar = self.bar(_five_by_1km_plan())
        self.assertEqual(1, bar.count('title="熱身"'))
        self.assertEqual(5, bar.count('title="門檻配速"'))
        self.assertEqual(5, bar.count('title="恢復"'))
        self.assertEqual(1, bar.count('title="緩和"'))
        self.assertEqual(12, bar.count("<i "))

    def test_a_repeats_count_does_not_carry_into_the_following_top_level_step(self):
        # Defect 2: the deleted parser kept `repeat = N` as loop state until another
        # non-dash line changed it, so a top-level step right after a repeat block
        # drew N times instead of once.
        plan = {
            "kind": "time_axis",
            "name": "intervals + cooldown",
            "steps": [
                {
                    "kind": "repeat",
                    "repetitions": 3,
                    "steps": [{
                        "kind": "work",
                        "name": "interval",
                        "duration": {"kind": "time", "seconds": 120},
                        "target": {
                            "kind": "pace", "unit": "sec_per_km",
                            "low_seconds_per_km": 180, "high_seconds_per_km": 190,
                        },
                    }],
                },
                {
                    "kind": "work",
                    "name": "cooldown",
                    "duration": {"kind": "time", "seconds": 300},
                    "target": {"kind": "open"},
                },
            ],
        }
        bar = self.bar(plan)
        self.assertEqual(3, bar.count('title="interval"'))
        self.assertEqual(1, bar.count('title="cooldown"'))

    def test_a_duration_like_token_in_a_step_name_never_changes_its_duration(self):
        # Defect 3, and the live #75 incident: the deleted regex read the last
        # number+unit token anywhere in the rendered line, so a name like "門檻
        # 1000m" was read as 1000 minutes. Duration must come only from the
        # `duration` field -- proven by giving two steps the identical 60s duration
        # and checking that the ambiguous name still splits the bar exactly evenly.
        plan = {
            "kind": "time_axis",
            "name": "ambiguous name",
            "steps": [
                {
                    "kind": "work",
                    "name": "門檻 1000m 5x 90s",
                    "duration": {"kind": "time", "seconds": 60},
                    "target": {"kind": "open"},
                },
                {
                    "kind": "work",
                    "name": "plain",
                    "duration": {"kind": "time", "seconds": 60},
                    "target": {"kind": "open"},
                },
            ],
        }
        bar = self.bar(plan)
        widths = re.findall(r'width:([\d.]+)%', bar)
        self.assertEqual(["50.00", "50.00"], widths)

    def test_pace_and_open_targets_map_from_structured_fields_not_prose(self):
        plan = {
            "kind": "time_axis",
            "name": "mixed targets",
            "steps": [
                {
                    "kind": "work",
                    "name": "快",
                    "duration": {"kind": "time", "seconds": 60},
                    "target": {
                        "kind": "pace", "unit": "sec_per_km",
                        "low_seconds_per_km": 180, "high_seconds_per_km": 190,
                    },
                },
                {
                    "kind": "work",
                    "name": "慢",
                    "duration": {"kind": "time", "seconds": 60},
                    "target": {"kind": "open"},
                },
            ],
        }
        bar = self.bar(plan, threshold_sec=230)
        # 185s/km average pace is 45s faster than the 230s/km threshold: z5.
        self.assertIn('background:var(--z5)" title="快"', bar)
        self.assertIn('background:var(--z1)" title="慢"', bar)

    def test_hr_ceiling_targets_map_from_structured_fields_not_prose(self):
        # hr_ceiling is validated to stand alone (never inside a repeat, never mixed
        # with a pace target), so this checks it renders as its own step rather than
        # vanishing or being misread as a pace or percent-HR target.
        bar = self.bar({
            "kind": "time_axis",
            "name": "capped easy run",
            "steps": [{
                "kind": "work",
                "name": "Easy run",
                "duration": {"kind": "time", "seconds": 1800},
                "target": {"kind": "hr_ceiling", "unit": "bpm", "ceiling_bpm": 140},
            }],
        })
        self.assertEqual(1, bar.count("<i "))
        self.assertIn("width:100.00%", bar)
        self.assertIn('title="Easy run"', bar)

    def _open_recovery_plan(self) -> dict:
        """Issue #18's own scenario: 5 x (1000m at a pace + 400m open recovery)."""
        return {
            "kind": "time_axis",
            "name": "5x1km with open recovery",
            "steps": [{
                "kind": "repeat",
                "repetitions": 5,
                "steps": [
                    {
                        "kind": "work",
                        "name": "1000m 門檻",
                        "duration": {"kind": "distance", "meters": 1000},
                        "target": {
                            "kind": "pace", "unit": "sec_per_km",
                            "low_seconds_per_km": 200, "high_seconds_per_km": 210,
                        },
                    },
                    {
                        "kind": "work",
                        "name": "400m 慢跑",
                        "duration": {"kind": "distance", "meters": 400},
                        "target": {"kind": "open"},
                    },
                ],
            }],
        }

    def test_an_open_distance_step_claims_no_share_of_the_week_s_time(self):
        """Issue #18: 400m of open recovery became 400 *seconds* with no baseline, so a
        recovery jog drew nearly twice the 1000m interval it followed.

        What the plan states is the work step's own pace, so the five paced kilometres
        still divide the bar between them; the recoveries render at a fixed width that
        is not a time share at all.
        """
        bar = self.bar(self._open_recovery_plan(), threshold_sec=None)

        self.assertEqual(10, bar.count("<i "))
        self.assertEqual(5, bar.count('class="u"'))
        self.assertEqual(["20.00"] * 5, re.findall(r'width:([\d.]+)%', bar))

    def test_an_open_distance_step_never_borrows_the_threshold_pace(self):
        # The threshold pace is an intensity this step deliberately did not prescribe,
        # so having measured one may not change how long the plan is drawn as taking.
        without_baseline = self.bar(self._open_recovery_plan(), threshold_sec=None)
        with_baseline = self.bar(self._open_recovery_plan(), threshold_sec=370)

        self.assertEqual(
            re.findall(r'width:([\d.]+)%', without_baseline),
            re.findall(r'width:([\d.]+)%', with_baseline),
        )

    def test_an_unknown_duration_is_not_zero_and_does_not_vanish(self):
        # `None` is the unknown, not `0.0`: a zero would be filtered straight back out
        # by the width filter, which is defect 1 arriving through the other door.
        bar = self.bar({
            "kind": "time_axis",
            "name": "open distance only",
            "steps": [{
                "kind": "work",
                "name": "慢跑",
                "duration": {"kind": "distance", "meters": 400},
                "target": {"kind": "open"},
            }],
        }, threshold_sec=230)

        self.assertEqual(1, bar.count("<i "))
        self.assertIn('class="u"', bar)
        self.assertNotIn("width:", bar)

    def test_a_duration_that_cannot_be_read_is_unknown_rather_than_zero(self):
        # The schema admits neither of these, so both branches are defensive -- but a
        # duration this code cannot read is the same answer as one the plan never
        # stated, and answering it with zero is the one thing that drops the step out
        # of the bar (AGENTS.md 3).
        for duration in (
            {"kind": "time", "seconds": "ten minutes"},
            {"kind": "distance", "meters": None},
        ):
            with self.subTest(duration=duration):
                bar = self.bar({
                    "kind": "time_axis",
                    "name": "unreadable duration",
                    "steps": [
                        {
                            "kind": "work",
                            "name": "壞掉的段落",
                            "duration": duration,
                            "target": {"kind": "open"},
                        },
                        {
                            "kind": "work",
                            "name": "好的段落",
                            "duration": {"kind": "time", "seconds": 600},
                            "target": {"kind": "open"},
                        },
                    ],
                })

                self.assertEqual(2, bar.count("<i "))
                self.assertEqual(1, bar.count('class="u"'))
                # And the readable step still owns the whole time share.
                self.assertEqual(["100.00"], re.findall(r'width:([\d.]+)%', bar))

    def test_a_paced_distance_step_keeps_its_supported_time_conversion(self):
        # 1000m at 200-210 sec/km is 205 seconds by the plan's own number, so it must
        # render exactly as a 205-second time step beside it.
        bar = self.bar({
            "kind": "time_axis",
            "name": "paced distance beside its own time",
            "steps": [
                {
                    "kind": "work",
                    "name": "距離",
                    "duration": {"kind": "distance", "meters": 1000},
                    "target": {
                        "kind": "pace", "unit": "sec_per_km",
                        "low_seconds_per_km": 200, "high_seconds_per_km": 210,
                    },
                },
                {
                    "kind": "work",
                    "name": "時間",
                    "duration": {"kind": "time", "seconds": 205},
                    "target": {"kind": "open"},
                },
            ],
        }, threshold_sec=230)

        self.assertEqual(["50.00", "50.00"], re.findall(r'width:([\d.]+)%', bar))

    def test_an_unstructured_session_renders_no_structure_bar(self):
        self.assertEqual("", self.bar({"kind": "unstructured"}))

    def test_a_movement_list_session_renders_no_structure_bar(self):
        self.assertEqual("", self.bar({
            "kind": "movement_list",
            "movements": [{
                "exercise": "back_squat",
                "display_name": "分腿蹲",
                "sets": 5,
                "reps": 5,
                "load_kg": 60,
                "assist_kg": None,
                "load_basis": "measured_baseline",
            }],
        }))

    def test_step_names_are_html_escaped(self):
        bar = self.bar({
            "kind": "time_axis",
            "name": "escaping",
            "steps": [{
                "kind": "work",
                "name": '<b>hard</b> & "fast"',
                "duration": {"kind": "time", "seconds": 60},
                "target": {"kind": "open"},
            }],
        })
        self.assertIn("&lt;b&gt;hard&lt;/b&gt; &amp; &quot;fast&quot;", bar)
        self.assertNotIn("<b>hard</b>", bar)

    def test_the_same_planstate_draws_the_same_bar_with_or_without_the_events_file(self):
        # Provider read-back is delivery evidence, not a second statement of the
        # workout's structure (archived issue #118 acceptance).
        session = _completed_running_session()
        session["plan"] = _five_by_1km_plan()
        without_events = preview.render_session_row(session, {}, 370, today=None)
        bogus_events = {
            "128000000": {"name": "wrong", "description": "- totally different workout 99m"}
        }
        with_events = preview.render_session_row(session, bogus_events, 370, today=None)
        self.assertEqual(without_events, with_events)


class PageProjectionTests(unittest.TestCase):
    """The page may report the plan. It may not forecast on the plan's behalf."""

    def setUp(self):
        self.plan = load(EXAMPLE / "plan-state-v2-day-4.json")
        self.event = load(EXAMPLE / "decision-event-day-4.json")
        self.today = date(2026, 8, 13)

    def render(self, plan: dict | None = None) -> str:
        return preview.render_page(plan or self.plan, self.event, {}, self.today)

    def test_the_week_says_what_a_structure_bar_s_widths_mean(self):
        # Issue #18 criterion 6: a bar whose widths are a time share has to say so, and
        # say that the fixed slivers beside them are not one.
        html = self.render()

        self.assertIn('class="sbar"', html)
        self.assertIn("結構條的寬度是課表寫下的時間比例", html)
        self.assertIn("不佔任何時間比例", html)

    def test_a_week_with_no_structure_bar_makes_no_claim_about_one(self):
        # The caption explains a bar; a week of sessions that draw none (a strength-only
        # week) must not carry an explanation of something the page never showed.
        plan = copy.deepcopy(self.plan)
        for session in plan["week"]["sessions"]:
            session["plan"] = {"kind": "unstructured"}
        html = self.render(plan)

        self.assertNotIn('class="sbar"', html)
        self.assertNotIn("結構條的寬度", html)

    def test_no_hard_coded_measurement_date(self):
        self.assertNotIn("8/05", self.render())

    def test_no_projected_threshold_gain(self):
        html = self.render()
        threshold = self.plan["athlete_baseline"]["threshold_pace_sec_per_km"]
        self.assertIn(preview.pace_str(threshold), html)  # the measured value may show
        for gain in range(1, 61):  # any faster pace would be a forecast
            self.assertNotIn(preview.pace_str(threshold - gain), html)

    def test_the_quality_run_count_comes_from_the_plan(self):
        plan = copy.deepcopy(self.plan)
        for session in plan["week"]["sessions"]:
            if session["session_id"] == "run-quality-01":
                session["adaptation"] = "threshold"
                session["match_status"] = "completed"
        self.assertIn("1 / 1", self.render(plan))
        self.assertNotIn("0 / 4", self.render(plan))

    def test_an_unpublished_week_never_claims_the_watch(self):
        # Every session in the fixture is not_published; the page must say so.
        self.assertNotIn("已在錶上", self.render())

    def test_the_goal_and_exit_conditions_are_read_from_the_plan(self):
        html = self.render()
        self.assertIn(self.plan["goal"]["outcome"], html)
        for condition in self.plan["cycle"]["stop_conditions"]:
            self.assertIn(condition, html)

    def test_a_publishable_strength_day_is_listed_beside_the_runs_to_deliver(self):
        # Strength reaches the calendar as a titled entry, so it is waiting to be
        # delivered exactly as a run is. Filtering this list on sport left the athlete
        # reading "nothing to deliver" on a week whose strength days were all pending.
        plan = copy.deepcopy(self.plan)
        strength = next(
            session for session in plan["week"]["sessions"]
            if session["sport"] == "strength" and session["match_status"] == "planned"
        )
        strength["execution"]["publish_supported"] = True

        html = self.render(plan)

        self.assertIn(strength["purpose"], html)
        self.assertNotIn("本週沒有待交付", html)


if __name__ == "__main__":
    unittest.main()
