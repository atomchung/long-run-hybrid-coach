"""Recovery corrections and day retractions through the public MCP route.

The athlete can correct a number without clearing the others, or take back the whole
day without losing another day, another account, or the provider's observations.
"""

from __future__ import annotations

import unittest

from garmin_coach_loop import athlete_evidence
from garmin_coach_loop.mcp_transport import TOOLS_BY_NAME

from test_gateway import TOKEN_A, TOKEN_B, recovery_signals_upload
from test_mcp_output_contract import OutputContractCase, schema_violations


class RecoveryRecordLifecycleTests(OutputContractCase):
    def record(self, *days, token=TOKEN_A):
        result = self.tool_result(
            "startCoachSession",
            {"recovery_signals": {"source": "watch-face", "days": list(days)}},
            token=token,
        )
        self.assertNotEqual(True, result.get("isError"), result)
        payload = self.tool_payload(result)
        self.assertEqual(
            [], schema_violations(payload, TOOLS_BY_NAME["startCoachSession"].output_schema)
        )
        return payload

    def retract(self, date="2026-08-12", **extra):
        return self.checked(
            "retractAthleteRecord", {"kind": "recovery_reading", "date": date, **extra}
        )

    def test_correction_preserves_unstated_values_and_discloses_persistence(self):
        first = self.record({
            "date": "2026-08-12", "hrv_last_night_ms": 62,
            "sleep_score": 74, "sleep_duration_sec": 25200, "resting_hr_bpm": 48,
        })
        self.assertEqual(
            {"stored_dates": ["2026-08-12"], "corrected_dates": [], "source": "athlete_reported"},
            first["recovery_recording"],
        )
        corrected = self.record({
            "date": "2026-08-12", "hrv_last_night_ms": 63, "sleep_score": None,
        })
        self.assertEqual(["2026-08-12"], corrected["recovery_recording"]["corrected_dates"])
        replay = self.record({"date": "2026-08-12", "hrv_last_night_ms": 63})
        self.assertEqual([], replay["recovery_recording"]["corrected_dates"])

        later = self.checked("startCoachSession")
        self.assertNotIn("recovery_recording", later)
        self.assertEqual([{
            "date": "2026-08-12", "hrv_last_night_ms": 63,
            "sleep_score": 74, "sleep_duration_sec": 25200, "resting_hr_bpm": 48,
            "source": "athlete_reported",
        }], later["context"]["reported_recovery"]["days"])

    def test_retraction_removes_only_the_named_day_and_echoes_its_values(self):
        self.record(
            {"date": "2026-08-11", "hrv_last_night_ms": 61},
            {"date": "2026-08-12", "hrv_last_night_ms": 62, "sleep_score": 74},
        )
        self.checked("recordBodyMeasurement", {"date": "2026-08-12", "weight_kg": 72})
        before_plan = self.checked("getCoachState")
        provider_calls = list(self.fake.calls)

        removed = self.retract()
        self.assertTrue(removed["retracted"])
        self.assertEqual(1, removed["record_count"])
        self.assertEqual({
            "date": "2026-08-12", "hrv_last_night_ms": 62, "sleep_score": 74,
            "sleep_duration_sec": None, "resting_hr_bpm": None, "source": "athlete_reported",
        }, removed["removed"])
        self.assertIsNone(removed["on_record_that_day"])
        self.assertEqual([], removed["candidates"])
        self.assertEqual(provider_calls, self.fake.calls)
        self.assertEqual(before_plan, self.checked("getCoachState"))
        self.assertEqual(1, len(athlete_evidence.load_evidence(self.state_dir)["body_measurements"]))
        later = self.checked("startCoachSession")
        self.assertEqual(
            ["2026-08-11"], [row["date"] for row in later["context"]["reported_recovery"]["days"]]
        )

    def test_repeated_removal_leaves_missing_unknown_and_can_be_restated(self):
        self.record({"date": "2026-08-12", "hrv_last_night_ms": 62})
        self.retract()
        repeated = self.retract()
        self.assertTrue(repeated["retracted"])
        self.assertIsNone(repeated["removed"])
        self.assertEqual(0, repeated["record_count"])
        self.assertIn("2026-08-12", repeated["note"])
        later = self.checked("startCoachSession")
        self.assertIsNone(later["context"]["reported_recovery"])
        self.assertIsNone(later["context"]["recovery_signals"])
        self.assertTrue(any("recovery" in note for note in later["unknowns"]))

        self.record({"date": "2026-08-12", "sleep_score": 74})
        later = self.checked("startCoachSession")
        self.assertIsNone(later["context"]["reported_recovery"]["days"][0]["hrv_last_night_ms"])

    def test_provider_observations_and_another_accounts_record_survive(self):
        other_owner = self.seed_owner(TOKEN_B, athlete_id="i2")
        self.record({"date": "2026-08-12", "hrv_last_night_ms": 70}, token=TOKEN_B)
        other_path = self.owner_dir(other_owner)
        other_before = self.snapshot(other_path)
        self.record({"date": "2026-08-12", "hrv_last_night_ms": 62})
        self.fake.wellness = [{"id": "2026-08-12", "hrv": 58.0, "sleepScore": 72}]
        provider_before = list(self.fake.wellness)

        self.retract()
        later = self.checked("startCoachSession")
        self.assertIsNone(later["context"]["reported_recovery"])
        self.assertEqual(58.0, later["context"]["recovery_signals"]["days"][0]["hrv_last_night_ms"])
        self.assertEqual(provider_before, self.fake.wellness)
        self.assertEqual(other_before, self.snapshot(other_path))

    def test_retraction_and_receipt_work_before_the_first_plan(self):
        self.owner_id = self.seed_owner(TOKEN_B, athlete_id="i2")
        self.state_dir = self.owner_dir(self.owner_id)
        first = self.record({"date": "2026-08-12", "hrv_last_night_ms": 62}, token=TOKEN_B)
        self.assertEqual("no_plan_state", first["status"])
        self.assertEqual(["2026-08-12"], first["recovery_recording"]["stored_dates"])
        removed = self.tool_payload(self.tool_result(
            "retractAthleteRecord", {"kind": "recovery_reading", "date": "2026-08-12"}, token=TOKEN_B,
        ))
        self.assertEqual(62, removed["removed"]["hrv_last_night_ms"])
        self.assertNotIn("reading_id", removed["removed"])
        self.assertFalse((self.state_dir / "store.json").exists())
        self.assertEqual([], athlete_evidence.load_evidence(self.state_dir)["reported_recovery"])

    def test_old_imported_readings_retract_outside_the_context_window(self):
        imported = self.checked("importAthleteHistory", {
            "format": "apple_health_xml",
            "content": '<HealthData><Record type="HKQuantityTypeIdentifierRestingHeartRate" '
                       'unit="count/min" startDate="2026-06-20 05:30:00 +0800" value="47"/></HealthData>',
        })
        self.assertEqual("2026-06-20", imported["recovery_added"]["items"][0]["date"])
        self.assertIsNone(self.checked("startCoachSession")["context"]["reported_recovery"])
        removed = self.retract("2026-06-20")
        self.assertEqual("athlete_imported", removed["removed"]["source"])
        self.assertEqual(47, removed["removed"]["resting_hr_bpm"])
        self.assertEqual(0, removed["record_count"])
        self.assertNotIn("reading_id", removed["removed"])

    def test_default_date_uses_the_athletes_timezone(self):
        self.record({"date": "2026-08-13", "hrv_last_night_ms": 62})
        other_day = self.checked("retractAthleteRecord", {
            "kind": "recovery_reading", "timezone": "America/Los_Angeles",
        })
        self.assertIsNone(other_day["removed"])
        self.assertIn("2026-08-12", other_day["note"])
        today = self.checked("retractAthleteRecord", {"kind": "recovery_reading"})
        self.assertEqual("2026-08-13", today["removed"]["date"])

    def test_invalid_date_or_a_restatement_disguised_as_removal_changes_nothing(self):
        self.record({"date": "2026-08-12", "hrv_last_night_ms": 62})
        before = self.snapshot(self.state_dir)
        for arguments in (
            {"date": "2026-08-14"}, {"date": "yesterday"},
            {"date": "2026-08-12", "hrv_last_night_ms": 63},
            {"date": "2026-08-12", "sport": "running"},
        ):
            with self.subTest(arguments=arguments):
                result = self.tool_result("retractAthleteRecord", {"kind": "recovery_reading", **arguments})
                self.assertTrue(result.get("isError"), result)
                self.assertEqual(before, self.snapshot(self.state_dir))

    def test_empty_or_transient_only_uploads_do_not_claim_persistence_or_clear_readings(self):
        self.record({"date": "2026-08-12", "hrv_last_night_ms": 62})
        before = self.snapshot(self.state_dir)
        for upload in (
            {"source": "watch-face", "days": []}, recovery_signals_upload(),
        ):
            with self.subTest(upload=upload):
                payload = self.checked("startCoachSession", {"recovery_signals": upload})
                self.assertNotIn("recovery_recording", payload)
                self.assertEqual(before, self.snapshot(self.state_dir))


if __name__ == "__main__":
    unittest.main()
