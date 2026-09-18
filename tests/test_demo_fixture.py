"""The demo athlete is contract-shaped, deliberately incomplete, and nobody real.

Three separate claims, and the fixture is only safe to serve publicly if all three hold.

*Contract-shaped*: both files go through the product's own validators. The demo reuses the
gateway's evidence projection and the product's plan-change projector, and a fixture that
has drifted past ``contracts/`` is one those readers would answer from wrongly rather than
refuse -- so the drift is caught here, where a renamed field fails a test instead of
quietly changing what the demo says.

*Deliberately incomplete*: the gaps are the point. A fixture where everything is populated
teaches the demo that evidence is always there, and AGENTS.md 3 is the rule it would then
break in public. Each gap this fixture keeps is asserted, because "we removed that field to
make the demo cleaner" is exactly the change that would pass review otherwise.

*Nobody real*: no name, no address, no account, no contact detail, no provider id. The
repository safety gate scans committed files for several of these already; what it cannot
know is that this particular file is the one served to anonymous callers, which is why the
scan is repeated here against the fixture's own content.
"""

from __future__ import annotations

import json
import re
import unittest
from pathlib import Path

from entrypoints.demo import fixture
from garmin_coach_loop import context_view, validation


ROOT = Path(__file__).resolve().parents[1]


class DemoFixtureContractTest(unittest.TestCase):
    def test_both_fixtures_validate_against_the_product_contracts(self):
        report = fixture.validate()
        self.assertEqual([], report["errors"])

    def test_the_context_declares_the_current_schema_version(self):
        self.assertEqual(
            validation.COACH_CONTEXT_SCHEMA_VERSION, fixture.context()["schema_version"]
        )
        self.assertEqual(
            validation.PLAN_STATE_SCHEMA_VERSION, fixture.plan_state()["schema_version"]
        )

    def test_the_context_and_the_plan_name_the_same_plan(self):
        context, plan = fixture.context(), fixture.plan_state()
        self.assertEqual(plan["plan_id"], context["goal_context"]["plan_id"])
        self.assertEqual(plan["version"], context["goal_context"]["plan_version"])

    def test_every_evidence_group_projects(self):
        # The demo hands the model a default read and offers the rest through
        # readDemoEvidence. A group that cannot be sliced is one of those offers failing at
        # the moment a visitor takes it up.
        for group in context_view.ALL_GROUPS:
            with self.subTest(group=group):
                fields, holds = fixture.evidence_slice([group])
                self.assertIn(group, holds)
                self.assertIsInstance(fields, dict)

    def test_a_fixture_read_is_a_copy_a_caller_cannot_write_through(self):
        first = fixture.context()
        first["unknowns"].append("mutated")
        self.assertNotIn("mutated", fixture.context()["unknowns"])
        plan = fixture.plan_state()
        plan["week"]["sessions"].clear()
        self.assertTrue(fixture.plan_state()["week"]["sessions"])


class DemoFixtureContentTest(unittest.TestCase):
    """What the fixture has to contain for the demo's own acceptance turns to be possible."""

    def setUp(self):
        self.context = fixture.context()
        self.plan = fixture.plan_state()

    def test_it_carries_between_four_and_six_weeks_of_running_history(self):
        weeks = [
            row
            for row in self.context["baseline_evidence"]
            if row["field"] == "weekly_volume_km_4wk_avg"
        ][0]["observed"]["weeks"]
        self.assertGreaterEqual(len(weeks), 4)
        self.assertLessEqual(len(weeks), 6)
        self.assertTrue(all(week["runs"] > 0 for week in weeks))

    def test_it_carries_comparable_quality_sessions(self):
        quality = [
            session
            for session in self.context["cycle_sessions"]
            if session["sport"] == "running" and session["cost"] == "hard"
        ]
        # Comparable means the same prescription repeated, not merely three hard days.
        threshold = [
            session for session in quality if "Threshold" in (session["prescription"] or "")
        ]
        self.assertGreaterEqual(len(threshold), 2)
        self.assertEqual(1, len({session["prescription"] for session in threshold}))
        segments = self.context["segment_execution"]["activities"]
        self.assertGreaterEqual(len(segments), 2)

    def test_it_carries_easy_and_long_run_exposure(self):
        prescriptions = [session["prescription"] or "" for session in self.context["cycle_sessions"]]
        self.assertTrue(any(line.startswith("Easy run") for line in prescriptions))
        self.assertTrue(any(line.startswith("Long easy run") for line in prescriptions))

    def test_it_carries_two_strength_movements_with_execution_evidence(self):
        movements = {
            movement["exercise"] for movement in self.context["movement_history"]["movements"]
        }
        self.assertGreaterEqual(len(movements), 2)
        logged = {
            session["exercise"] for session in self.context["strength_execution"]["sessions"]
        }
        self.assertEqual(movements, logged)
        self.assertTrue(
            any(
                any(entry["reps"] < 5 for entry in session["sets"])
                for session in self.context["strength_execution"]["sessions"]
                if session["exercise"] == "back squat"
            ),
            "one set is short on purpose -- a log where every set went as written teaches "
            "the demo that execution always matches the prescription",
        )

    def test_it_carries_one_running_goal_and_one_strength_maintenance_goal(self):
        self.assertIn("10K", self.context["goal_context"]["primary_goal"])
        self.assertEqual("strength", self.context["goal_context"]["maintenance_goal"])
        self.assertEqual("strength", self.plan["cycle"]["maintenance_adaptation"])

    def test_it_carries_the_three_by_forty_five_constraint_for_next_week(self):
        constraints = self.context["constraints"]
        self.assertEqual(45, constraints["session_minutes"])
        self.assertEqual(3, len(constraints["available_days"]))
        stated = " ".join(constraints["week_constraints"])
        self.assertIn("three sessions", stated)
        self.assertIn("45 minutes", stated)

    def test_the_gaps_are_real_gaps_rather_than_zeroes(self):
        context = self.context
        self.assertGreaterEqual(len(context["unknowns"]), 4)

        # A prescribed strength session nobody logged: no evidence, and a status that was
        # not derived from that absence.
        unlogged = [
            session
            for session in context["cycle_sessions"]
            if session["activity_evidence"] == "none_found"
        ]
        self.assertTrue(unlogged)
        for session in unlogged:
            self.assertIsNone(session["activity"])
            self.assertNotIn(session["match_status"], {"completed", "missed"})

        # Overnight HRV on some nights and not others, reported as partial coverage.
        self.assertEqual("partial", context["coverage"]["hrv"]["status"])
        self.assertLess(
            context["coverage"]["hrv"]["observed_days"],
            context["coverage"]["hrv"]["expected_days"],
        )
        self.assertEqual("unknown", context["recovery_trends"]["hrv"]["status"])
        self.assertTrue(
            any(
                day["hrv_last_night_ms"] is None
                for day in context["recovery_signals"]["days"]
            ),
            "a night with no reading is null, never 0",
        )

        # The declared outcome measurement is declared and has not been run.
        self.assertIsNotNone(context["goal_context"]["measurement"])
        self.assertEqual("attached", context["measurement_evidence"]["reference_result"])
        self.assertNotEqual("attached", context["measurement_evidence"]["comparison_result"])
        self.assertIsNone(context["measurement_evidence"]["comparison_session_id"])

        # Two of the three comparable sessions carry no per-segment heart rate.
        detailed = [
            activity
            for activity in context["segment_execution"]["activities"]
            if "segments" in activity
        ]
        compact = [
            activity
            for activity in context["segment_execution"]["activities"]
            if "segment_rows" in activity
        ]
        self.assertTrue(detailed)
        self.assertTrue(compact)

    def test_the_manifest_lists_the_gaps_it_keeps(self):
        manifest = fixture.manifest()
        self.assertFalse(manifest["private_data"])
        self.assertFalse(manifest["provider_connection"])
        self.assertGreaterEqual(len(manifest["deliberate_unknowns"]), 4)


class DemoFixtureIdentityTest(unittest.TestCase):
    """Nothing in the fixture belongs to a person."""

    def setUp(self):
        self.text = "\n".join(
            path.read_text(encoding="utf-8") for path in sorted(fixture.FIXTURE_DIR.glob("*.json"))
        )

    def test_it_contains_no_contact_detail_or_account_handle(self):
        # The home-path pattern is assembled rather than spelled, because
        # scripts/check_repo_safety.py scans this file too and a literal one here would be
        # the very thing it refuses.
        home = "/" + "Users" + "/"
        patterns = {
            "email address": r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}",
            "phone number": r"\+\d[\d\s().-]{7,}",
            "home path": f"(?:{home}|/home/[a-z]+/|[A-Za-z]:\\\\Users\\\\)",
            "http url": r"https?://",
        }
        for label, pattern in patterns.items():
            with self.subTest(label=label):
                self.assertIsNone(
                    re.search(pattern, self.text),
                    f"the public demo fixture carries a possible {label}",
                )

    def test_every_identifier_in_it_is_a_demo_identifier(self):
        context = fixture.context()
        self.assertTrue(context["context_id"].startswith("demo-"))
        self.assertTrue(fixture.plan_state()["plan_id"].startswith("demo-"))
        for source in context["sources"]:
            self.assertEqual("offline", source["mode"])
            self.assertTrue(source["sanitized"])
        for session in context["cycle_sessions"]:
            if session["activity"] is not None:
                self.assertTrue(session["activity"]["activity_id"].startswith("demo-"))

    def test_the_context_declares_itself_free_of_provider_material(self):
        privacy = fixture.context()["privacy"]
        self.assertTrue(privacy["sanitized"])
        for field in (
            "contains_raw_payloads",
            "contains_credentials",
            "contains_gps_tracks",
            "contains_connection_state",
        ):
            self.assertFalse(privacy[field])

    def test_no_session_in_it_was_ever_delivered_anywhere(self):
        for session in fixture.plan_state()["week"]["sessions"]:
            self.assertEqual("not_published", session["execution"]["delivery_state"])
            self.assertIsNone(session["execution"]["external_id"])


class DemoAcceptancePromptTest(unittest.TestCase):
    def test_the_three_acceptance_turns_are_committed_beside_the_fixture(self):
        prompts = fixture.acceptance_prompts()
        self.assertEqual(3, len(prompts))
        for prompt in prompts:
            with self.subTest(prompt=prompt["id"]):
                self.assertTrue(prompt["message"].strip())
                self.assertTrue(prompt["expects"])

    def test_the_three_turn_conversation_is_committed_beside_them(self):
        conversation = fixture.acceptance_conversation()
        turns = conversation["turns"]
        self.assertEqual(3, len(turns))
        self.assertEqual(3, len({turn["id"] for turn in turns}), "turn ids are the report's keys")
        for turn in turns:
            with self.subTest(turn=turn["id"]):
                self.assertTrue(turn["message"].strip())
                self.assertTrue(turn["expects"])
        # The property that makes this a continuity test rather than three more prompts: the
        # last two turns name something only an earlier turn said.
        self.assertIn("the second one", turns[1]["message"])
        self.assertIn("the first option", turns[2]["message"])

    def test_the_readme_quotes_the_hero_turn_as_committed(self):
        hero = fixture.acceptance_prompts()[0]["message"]
        readme = (ROOT / "entrypoints" / "demo" / "README.md").read_text(encoding="utf-8")
        self.assertIn(hero, readme)


if __name__ == "__main__":
    unittest.main()
