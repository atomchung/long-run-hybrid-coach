"""The public one-confirmation plan/calendar journey, including durable retries."""
from __future__ import annotations

import copy
import datetime as dt
import json
from unittest import mock

from garmin_coach_loop.delivery import owned_external_id_for
from garmin_coach_loop.gateway import CoachGateway
from garmin_coach_loop.proposals import open_proposal
from garmin_coach_loop.store import StateStoreError, canonical_hash, read_current_plan, pending_delivery_attempt, read_confirmed_delivery
from test_gateway import TOKEN_A, HMAC_KEY, ONBOARDING, RUN_SPORT_SETTINGS, WEEKLY_CHANGE, as_change_request, coaching_request, publishable_plan
from test_mcp_gateway import McpTestCase


class CombinedDecisionJourneyTests(McpTestCase):
    def setUp(self):
        super().setUp()
        self.plan = publishable_plan()
        self.owner_id = self.seed_owner(TOKEN_A, plan=self.plan)
        self.state_dir = self.owner_dir(self.owner_id)
        self.fake.sport_settings = copy.deepcopy(RUN_SPORT_SETTINGS)

    def tool(self, name, arguments=None):
        result = self.tool_result(name, arguments)
        self.assertFalse(result.get("isError"), result)
        return self.tool_payload(result)

    def prepare(self, change, **options):
        session = self.tool("startCoachSession", {"all_clear": True})
        for operation in change.get("sessions", []):
            plan = operation.get("plan") or {}
            if plan.get("kind") == "time_axis":
                self.fake.steps_by_name[plan["name"]] = plan["steps"]
        return self.tool("prepareCoachDecision", {
            "plan_id": session["plan_state"]["plan_id"],
            "plan_version": session["plan_state"]["plan_version"],
            "context": {"context_id": session["context"]["context_id"]},
            "change_request": change, **options,
        })

    def deliver(self, ids):
        current = read_current_plan(self.state_dir)
        prepared = self.tool("prepareWorkoutDelivery", {
            "plan_id": current["plan_id"], "plan_version": current["current_version"], "session_ids": ids,
        })
        return self.tool("applyWorkoutDelivery", {"proposal_hash": prepared["proposal_hash"], "confirmed": True})

    def apply(self, prepared, **extra):
        return self.tool("applyCoachDecision", {"proposal": prepared["proposal"], "confirmed": True, **extra})

    def restart(self):
        self.gateway = CoachGateway(self.config, fetch=self.fake, now=lambda: self.now)
        self.server.gateway = self.gateway

    def note_change(self):
        return coaching_request(sessions=[{"operation": "keep", "session_id": sid, "coach_note": "本週保持輕鬆"}
                                          for sid in ("run-quality-01", "run-long-01")])

    def early_week_roll(self):
        return coaching_request(week={"start": "2026-08-17", "intent": "提早決定下週"},
                                cycle={"outlook": self.plan["cycle"]["outlook"][1:]},
                                sessions=[{"operation": "add", "scheduled_date": "2026-08-17", "sport": "rest",
                                           "purpose": "換週休息", "adaptation": "recovery", "cost": "easy",
                                           "body_stress": "systemic", "priority": "flexible",
                                           "fallback": {"action": "rest", "description": "休息"},
                                           "planned_minutes": 0, "plan": {"kind": "unstructured"}}])

    def paired_activity(self, event_id):
        return {"id": "completed-retired", "type": "Run", "start_date_local": "2026-08-13T06:00:00",
                "moving_time": 3600, "distance": 10000, "average_speed": 10000 / 3600,
                "paired_event_id": event_id}

    def test_a_retired_completed_event_is_preserved_while_the_other_future_event_is_withdrawn(self):
        self.deliver(["run-quality-01", "run-long-01"])
        quality = next(e for e in self.fake.events if e["external_id"] == owned_external_id_for(self.plan, "run-quality-01"))
        original_event = copy.deepcopy(quality)
        prepared = self.prepare(self.early_week_roll())
        self.fake.calendar_status = 503
        self.apply(prepared)
        self.fake.calendar_status = None
        self.fake.activities = [self.paired_activity(quality["id"])]
        self.restart()
        resumed = self.tool("applyCoachDecision", {"proposal": prepared["proposal"]})
        self.assertEqual("passed", resumed["calendar_delivery"]["status"])
        self.assertEqual(["run-quality-01"], [s["session_id"] for s in resumed["calendar_delivery"]["skipped"]])
        self.assertEqual([{"session_id": "run-long-01"}], resumed["calendar_delivery"]["withdrawn"])
        self.assertEqual([original_event], self.fake.events)

    def assert_paired_completion_preserved(self, operation):
        self.deliver(["run-quality-01"])
        original_event = copy.deepcopy(self.fake.events[0])
        prepared = self.prepare(coaching_request(sessions=[{
            "session_id": "run-quality-01", **operation,
        }]))
        self.fake.calendar_status = 503
        self.apply(prepared)
        self.fake.calendar_status = None
        self.fake.activities = [self.paired_activity(original_event["id"])]
        self.restart()
        count = len(self.fake.bulk_calls)
        resumed = self.tool("applyCoachDecision", {"proposal": prepared["proposal"]})
        self.assertEqual(["run-quality-01"], [s["session_id"] for s in resumed["calendar_delivery"]["skipped"]])
        self.assertEqual([original_event], self.fake.events)
        self.assertEqual([], self.fake.deleted)
        self.assertEqual(count, len(self.fake.bulk_calls))

    def test_a_rest_replacement_preserves_the_old_event_when_its_actual_arrives(self):
        self.assert_paired_completion_preserved({
            "operation": "replace", "sport": "rest", "purpose": "今天休息",
            "adaptation": "recovery", "cost": "easy", "planned_minutes": 0,
            "plan": {"kind": "unstructured"},
        })

    def test_a_moved_replacement_preserves_the_old_event_when_its_actual_arrives(self):
        self.assert_paired_completion_preserved({"operation": "move", "scheduled_date": "2026-08-15"})

    def test_an_unpaired_same_day_run_does_not_protect_a_retired_calendar_target(self):
        self.deliver(["run-quality-01", "run-long-01"])
        prepared = self.prepare(self.early_week_roll())
        self.fake.calendar_status = 503
        self.apply(prepared)
        self.fake.calendar_status = None
        self.fake.activities = [self.paired_activity(None)]
        self.restart()
        resumed = self.tool("applyCoachDecision", {"proposal": prepared["proposal"]})
        self.assertEqual("passed", resumed["calendar_delivery"]["status"])
        self.assertEqual([], resumed["calendar_delivery"]["skipped"])
        self.assertEqual(2, len(resumed["calendar_delivery"]["withdrawn"]))
        self.assertEqual([], self.fake.events)

    def test_no_calendar_effect_or_plan_is_written_without_the_confirmation(self):
        self.deliver(["run-quality-01"])
        prepared = self.prepare(copy.deepcopy(WEEKLY_CHANGE))
        before, calls = self.snapshot(self.state_dir), len(self.fake.bulk_calls)
        refused = self.tool_result("applyCoachDecision", {"proposal": prepared["proposal"]})
        self.assertEqual("confirmation_required", self.tool_payload(refused)["error"])
        self.assertEqual(before, self.snapshot(self.state_dir))
        self.assertEqual(calls, len(self.fake.bulk_calls))

    def test_one_confirmation_replaces_the_delivered_workout_and_saves_the_plan(self):
        self.deliver(["run-quality-01"])
        event_id = str(self.fake.events[0]["id"])
        count = len(self.fake.bulk_calls)
        prepared = self.prepare(copy.deepcopy(WEEKLY_CHANGE))
        preview = prepared["preview"]["calendar_delivery"]
        self.assertEqual("replace", preview["workouts"][0]["operation"])
        self.assertEqual("2026-08-13", preview["workouts"][0]["workout"]["scheduled_date"])
        self.assertEqual(count, len(self.fake.bulk_calls))
        result = self.apply(prepared)
        self.assertEqual("passed", result["calendar_delivery"]["status"])
        self.assertEqual(1, len(self.fake.events))
        self.assertEqual(event_id, str(self.fake.events[0]["id"]))
        self.assertEqual(count + 1, len(self.fake.bulk_calls))
        current = read_current_plan(self.state_dir)
        session = next(s for s in current["current_plan"]["week"]["sessions"] if s["session_id"] == "run-quality-01")
        self.assertEqual(45, session["planned_minutes"])
        self.assertEqual("intervals_accepted", session["execution"]["delivery_state"])
        self.assertNotIn("superseded_external_id", session["execution"])

    def test_rest_withdraws_the_old_entry_under_that_same_confirmation(self):
        self.deliver(["run-quality-01"])
        change = coaching_request(sessions=[{
            "operation": "replace", "session_id": "run-quality-01", "sport": "rest",
            "purpose": "今天休息", "adaptation": "recovery", "cost": "easy", "planned_minutes": 0,
            "plan": {"kind": "unstructured"},
        }])
        prepared = self.prepare(change)
        self.assertEqual(1, len(prepared["preview"]["calendar_delivery"]["withdrawals"]))
        result = self.apply(prepared)
        self.assertEqual([{"session_id": "run-quality-01"}], result["calendar_delivery"]["withdrawn"])
        self.assertEqual([], self.fake.events)

    def test_partial_failure_resumes_the_saved_approval_after_cache_loss_without_another_yes(self):
        self.deliver(["run-quality-01", "run-long-01"])
        change = coaching_request(sessions=[{"operation": "keep", "session_id": sid, "coach_note": "本週保持輕鬆"}
                                             for sid in ("run-quality-01", "run-long-01")])
        prepared = self.prepare(change)
        owned = owned_external_id_for(self.plan, "run-quality-01")
        self.fake.corrupt_external_ids.add(owned)
        result = self.apply(prepared)
        self.assertEqual("partial", result["calendar_delivery"]["status"])
        self.assertTrue(result["calendar_delivery"]["attempt_open"])
        self.assertEqual("本週保持輕鬆", read_current_plan(self.state_dir)["current_plan"]["week"]["sessions"][3]["coach_note"])
        count = len(self.fake.bulk_calls)
        self.fake.corrupt_external_ids.clear()
        self.restart()
        resumed = self.tool("applyCoachDecision", {"proposal": prepared["proposal"]})
        self.assertTrue(resumed["idempotent_replay"])
        self.assertEqual("passed", resumed["calendar_delivery"]["status"])
        self.assertEqual(count, len(self.fake.bulk_calls))  # read-back closes the interrupted write
        self.assertEqual(2, len(self.fake.events))
        self.assertIsNone(pending_delivery_attempt(self.state_dir))

    def test_expired_item_does_not_prevent_the_other_approved_future_item(self):
        self.deliver(["run-quality-01", "run-long-01"])
        change = coaching_request(sessions=[{"operation": "keep", "session_id": sid, "coach_note": "本週保持輕鬆"}
                                             for sid in ("run-quality-01", "run-long-01")])
        prepared = self.prepare(change)
        self.fake.calendar_status = 503
        first = self.apply(prepared)
        self.assertEqual("partial", first["calendar_delivery"]["status"])
        self.assertIsNone(pending_delivery_attempt(self.state_dir))
        self.fake.calendar_status = None
        count = len(self.fake.bulk_calls)
        self.gateway._held.clear()
        self.now = dt.datetime(2026, 8, 14, tzinfo=dt.timezone.utc)
        resumed = self.tool("applyCoachDecision", {"proposal": prepared["proposal"]})
        self.assertEqual("passed", resumed["calendar_delivery"]["status"])
        self.assertEqual(["run-quality-01"], [s["session_id"] for s in resumed["calendar_delivery"]["skipped"]])
        self.assertEqual(count + 1, len(self.fake.bulk_calls))
        self.assertEqual("2026-08-16", self.fake.bulk_calls[-1]["start_date_local"][:10])

    def test_replayed_request_cannot_change_the_approved_content(self):
        self.deliver(["run-quality-01"])
        request = copy.deepcopy(WEEKLY_CHANGE)
        prepared = self.prepare(request)
        self.fake.calendar_status = 503
        self.apply(prepared)
        self.fake.calendar_status = None
        self.gateway._held.clear()
        count = len(self.fake.bulk_calls)
        altered = {**request, "summary": "A different request"}
        result = self.tool_result("applyCoachDecision", {"proposal": prepared["proposal"], "change_request": altered})
        self.assertTrue(result.get("isError"), result)
        self.assertEqual("proposal_mismatch", self.tool_payload(result)["error"])
        self.assertEqual(count, len(self.fake.bulk_calls))

    def test_a_different_current_prescription_cannot_reuse_a_saved_calendar_approval(self):
        self.deliver(["run-quality-01"])
        prepared = self.prepare(copy.deepcopy(WEEKLY_CHANGE))
        self.fake.calendar_status = 503
        self.apply(prepared)
        changed = self.prepare(coaching_request(sessions=[{
            "operation": "keep", "session_id": "run-quality-01", "coach_note": "另一個已確認的處方",
        }]))
        self.apply(changed)
        self.fake.calendar_status = None
        self.restart()
        count = len(self.fake.bulk_calls)
        resumed = self.tool("applyCoachDecision", {"proposal": prepared["proposal"]})
        self.assertEqual("partial", resumed["calendar_delivery"]["status"])
        self.assertIn("prescription differs", resumed["calendar_delivery"]["unresolved"][0]["reason"])
        self.assertEqual(count, len(self.fake.bulk_calls))

    def test_a_rehashed_receipt_cannot_change_what_the_signature_approved(self):
        self.deliver(["run-quality-01"])
        prepared = self.prepare(copy.deepcopy(WEEKLY_CHANGE))
        self.apply(prepared)
        claims = open_proposal(prepared["proposal"], key=HMAC_KEY, now=self.now)["claims"]
        receipt_path = next(path for path in (self.state_dir / "commits").glob("*/receipt.json")
                            if "confirmed_delivery" in json.loads(path.read_text()))
        receipt = json.loads(receipt_path.read_text())
        approval_key = receipt["confirmed_delivery"]["approval_key"]
        receipt["confirmed_delivery"]["prepared"]["effects"][0]["set"]["items"][0]["workout"]["scheduled_date"] = "2026-08-16"
        receipt.pop("receipt_hash")
        receipt["receipt_hash"] = canonical_hash(receipt)
        receipt_path.write_text(json.dumps(receipt))
        with self.assertRaisesRegex(StateStoreError, "approved effects"):
            read_confirmed_delivery(self.state_dir, approval_key=approval_key, claims=claims)

    def test_a_pending_write_that_became_history_can_finish_by_read_back_only(self):
        self.deliver(["run-quality-01", "run-long-01"])
        prepared = self.prepare(self.note_change())
        owned = owned_external_id_for(self.plan, "run-quality-01")
        self.fake.corrupt_external_ids.add(owned)
        self.assertTrue(self.apply(prepared)["calendar_delivery"]["attempt_open"])
        self.fake.corrupt_external_ids.clear()
        calls = len(self.fake.bulk_calls)
        self.now = dt.datetime(2026, 8, 14, tzinfo=dt.timezone.utc)
        self.restart()
        resumed = self.tool("applyCoachDecision", {"proposal": prepared["proposal"]})
        self.assertEqual("passed", resumed["calendar_delivery"]["status"])
        self.assertEqual(calls, len(self.fake.bulk_calls))
        self.assertIsNone(pending_delivery_attempt(self.state_dir))

    def test_identity_backed_execution_skips_only_that_approved_item(self):
        self.deliver(["run-quality-01", "run-long-01"])
        prepared = self.prepare(self.note_change())
        self.fake.calendar_status = 503
        self.apply(prepared)
        self.fake.calendar_status = None
        count = len(self.fake.bulk_calls)
        context = {"recent_actuals": [{"planned_session_id": "run-quality-01", "match_confidence": "owned", "completion": "partial"}]}
        with mock.patch.object(self.gateway, "_reread_evidence", return_value=(context, None, None)):
            resumed = self.tool("applyCoachDecision", {"proposal": prepared["proposal"]})
        self.assertEqual("passed", resumed["calendar_delivery"]["status"])
        self.assertEqual(["run-quality-01"], [s["session_id"] for s in resumed["calendar_delivery"]["skipped"]])
        self.assertEqual(count + 1, len(self.fake.bulk_calls))

    def test_unattached_actuals_do_not_invent_execution_or_block_the_approved_week(self):
        self.deliver(["run-quality-01", "run-long-01"])
        prepared = self.prepare(self.note_change())
        self.fake.calendar_status = 503
        self.apply(prepared)
        self.fake.calendar_status = None
        count = len(self.fake.bulk_calls)
        context = {"recent_actuals": [{"planned_session_id": "run-quality-01", "match_confidence": "candidate", "completion": "partial"}]}
        with mock.patch.object(self.gateway, "_reread_evidence", return_value=(context, None, None)):
            resumed = self.tool("applyCoachDecision", {"proposal": prepared["proposal"]})
        self.assertEqual([], resumed["calendar_delivery"]["skipped"])
        self.assertEqual(count + 2, len(self.fake.bulk_calls))

    def test_a_new_positive_symptom_holds_only_todays_delivery_for_a_human_decision(self):
        self.deliver(["run-quality-01", "run-long-01"])
        prepared = self.prepare(self.note_change())
        self.fake.calendar_status = 503
        self.apply(prepared)
        self.fake.calendar_status = None
        count = len(self.fake.bulk_calls)
        context = {"constraints": {"red_flags": {"pain": True}}}
        with mock.patch.object(self.gateway, "_reread_evidence", return_value=(context, None, None)):
            resumed = self.tool("applyCoachDecision", {"proposal": prepared["proposal"]})
        self.assertEqual(["run-quality-01"], [s["session_id"] for s in resumed["calendar_delivery"]["skipped"]])
        self.assertIn("human decision", resumed["calendar_delivery"]["skipped"][0]["reason"])
        self.assertEqual(count + 1, len(self.fake.bulk_calls))

    def test_an_exact_old_event_that_became_past_does_not_block_the_future_week(self):
        self.deliver(["run-quality-01", "run-long-01"])
        prepared = self.prepare(self.note_change())
        quality = next(e for e in self.fake.events if e["external_id"] == owned_external_id_for(self.plan, "run-quality-01"))
        quality["start_date_local"] = "2026-08-12T00:00:00"
        count = len(self.fake.bulk_calls)
        result = self.apply(prepared)
        self.assertEqual("passed", result["calendar_delivery"]["status"])
        self.assertEqual(["run-quality-01"], [s["session_id"] for s in result["calendar_delivery"]["skipped"]])
        self.assertEqual(count + 1, len(self.fake.bulk_calls))
        self.assertEqual("2026-08-12", quality["start_date_local"][:10])
        self.assertIsNone(pending_delivery_attempt(self.state_dir))

    def test_explicit_new_publication_keeps_its_confirmed_provider_setting_after_restart(self):
        self.fake.sport_settings = [{"types": ["Run"], "threshold_pace": None, "lthr": 163}]
        prepared = self.prepare(self.note_change(), publish_new_workouts=True)
        self.assertEqual(1, len(prepared["preview"]["calendar_delivery"]["settings_changes"]))
        self.fake.settings_write_status = 503
        first = self.apply(prepared)
        self.assertEqual("partial", first["calendar_delivery"]["status"])
        self.assertEqual([], self.fake.bulk_calls)
        self.fake.settings_write_status = None
        self.restart()
        resumed = self.tool("applyCoachDecision", {"proposal": prepared["proposal"]})
        self.assertEqual("passed", resumed["calendar_delivery"]["status"])
        self.assertEqual([{"threshold_pace": 2.702703}], self.fake.settings_updates)
        self.assertGreaterEqual(len(resumed["calendar_delivery"]["delivered"]), 2)

    def test_an_early_week_roll_withdraws_only_the_retired_future_deliveries(self):
        self.deliver(["run-quality-01", "run-long-01"])
        prepared = self.prepare(self.early_week_roll())
        self.assertEqual(2, len(prepared["preview"]["calendar_delivery"]["withdrawals"]))
        result = self.apply(prepared)
        self.assertEqual("passed", result["calendar_delivery"]["status"])
        self.assertEqual([], self.fake.events)
        self.assertEqual(["rest"], [s["sport"] for s in read_current_plan(self.state_dir)["current_plan"]["week"]["sessions"]])
        self.assertIsNone(pending_delivery_attempt(self.state_dir))

    def test_new_workouts_are_not_published_without_the_explicit_option(self):
        prepared = self.prepare(copy.deepcopy(WEEKLY_CHANGE))
        self.assertNotIn("calendar_delivery", prepared["preview"])
        self.apply(prepared)
        self.assertEqual([], self.fake.bulk_calls)

    def test_moved_evidence_repreviews_the_original_signed_publication_request(self):
        prepared = self.prepare(self.note_change(), publish_new_workouts=True)
        workouts = prepared["preview"]["calendar_delivery"]["workouts"]
        self.fake.activities = [{"id": "new-sync", "type": "Run", "start_date_local": "2026-08-12T06:00:00",
                                 "moving_time": 1800, "distance": 4000, "average_speed": 4000 / 1800}]
        refused = self.tool_payload(self.tool_result("applyCoachDecision", {
            "proposal": prepared["proposal"], "confirmed": True,
        }))
        self.assertEqual("proposal_superseded", refused["error"])
        again = refused["prepared"]
        self.assertTrue(again["confirmation_required"])
        self.assertEqual(workouts, again["preview"]["calendar_delivery"]["workouts"])
        self.assertEqual([], self.fake.bulk_calls)
        self.assertEqual(1, read_current_plan(self.state_dir)["current_version"])
        self.assertEqual("passed", self.apply(again)["calendar_delivery"]["status"])

    def test_moved_evidence_does_not_add_publication_to_a_plan_only_preview(self):
        prepared = self.prepare(self.note_change())
        self.fake.activities = [{"id": "new-sync", "type": "Run", "start_date_local": "2026-08-12T06:00:00",
                                 "moving_time": 1800, "distance": 4000, "average_speed": 4000 / 1800}]
        refused = self.tool_payload(self.tool_result("applyCoachDecision", {
            "proposal": prepared["proposal"], "confirmed": True,
        }))
        self.assertEqual("proposal_superseded", refused["error"])
        self.assertNotIn("calendar_delivery", refused["prepared"]["preview"])
        self.assertEqual([], self.fake.bulk_calls)


class FirstPlanCombinedJourneyTests(McpTestCase):
    def test_an_expired_first_plan_repreview_keeps_the_signed_publication_request(self):
        self.seed_owner(TOKEN_A)
        self.fake.sport_settings = copy.deepcopy(RUN_SPORT_SETTINGS)
        self.fake.register_plan_steps({"week": {"sessions": ONBOARDING["sessions"]}})
        request = as_change_request(copy.deepcopy(ONBOARDING))
        prepared = self.tool_payload(self.tool_result("prepareCoachDecision", {
            "change_request": request, "publish_new_workouts": True,
        }))
        workouts = prepared["preview"]["calendar_delivery"]["workouts"]
        self.now += dt.timedelta(hours=2)
        self.gateway._held.clear()
        refused = self.tool_payload(self.tool_result("applyCoachDecision", {
            "proposal": prepared["proposal"], "change_request": request, "confirmed": True,
        }))
        self.assertEqual("proposal_superseded", refused["error"])
        self.assertEqual(workouts, refused["prepared"]["preview"]["calendar_delivery"]["workouts"])
        self.assertEqual([], self.fake.bulk_calls)

    def test_one_first_plan_preview_can_include_first_delivery_and_survive_replay(self):
        self.seed_owner(TOKEN_A)
        self.fake.sport_settings = copy.deepcopy(RUN_SPORT_SETTINGS)
        self.fake.register_plan_steps({"week": {"sessions": ONBOARDING["sessions"]}})
        request = as_change_request(copy.deepcopy(ONBOARDING))
        result = self.tool_result("prepareCoachDecision", {"change_request": request, "publish_new_workouts": True})
        self.assertFalse(result.get("isError"), result)
        prepared = self.tool_payload(result)
        self.assertTrue(prepared["preview"]["calendar_delivery"]["workouts"])
        result = self.tool_result("applyCoachDecision", {"proposal": prepared["proposal"], "confirmed": True})
        self.assertFalse(result.get("isError"), result)
        applied = self.tool_payload(result)
        self.assertEqual("passed", applied["calendar_delivery"]["status"])
        count = len(self.fake.bulk_calls)
        self.gateway._held.clear()
        result = self.tool_result("applyCoachDecision", {"proposal": prepared["proposal"]})
        self.assertFalse(result.get("isError"), result)
        self.assertEqual(count, len(self.fake.bulk_calls))
