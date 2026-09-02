"""Reading a file the athlete uploaded, and writing it into the evidence they speak.

Two halves, tested apart because they fail apart. ``evidence_import`` reads bytes and
text into normalized rows and touches no store; ``athlete_evidence.import_reported_evidence``
takes rows and decides what is already on record. Everything a format knows lives in the
first half, so a fifth format would add tests here and none below it.

The FIT fixture is *built* rather than committed: AGENTS.md 2 keeps activity files out of
this repository, and a synthetic file exercises the decoder's framing -- header, definition
messages, base types, endianness -- which is the part that would silently misread.
"""

from __future__ import annotations

import base64
import datetime as dt
import struct
import tempfile
import unittest
from pathlib import Path

from garmin_coach_loop import evidence_import
from garmin_coach_loop.athlete_evidence import (
    ATHLETE_IMPORTED_SOURCE,
    ATHLETE_REPORTED_SOURCE,
    AthleteEvidenceError,
    import_reported_evidence,
    load_evidence,
    record_activity_summary,
    reported_activity_summaries,
    retract_activity_summary,
    same_reported_session,
)
from garmin_coach_loop.context_core import ContextRequest, build_window
from garmin_coach_loop.evidence_import import EvidenceImportError, read_payload


NOW = dt.datetime(2026, 8, 18, 9, 0, tzinfo=dt.timezone.utc)

STRAVA_CSV = """Activity ID,Activity Date,Activity Name,Activity Type,Elapsed Time,Distance
9001,2026-08-10 06:12:00,Morning Run,Run,2700,8.1
9002,2026-08-10 18:30:00,Evening Run,Run,1800,4.0
9003,2026-08-11 07:00:00,Pool,Swim,2400,1.2
"""


def _window():
    """The same temporal frame a real build runs against, built the same way."""
    request = ContextRequest(
        as_of_raw="2026-08-18T12:00:00+08:00",
        timezone_name="Asia/Taipei",
        available_days=[],
        session_minutes=None,
        red_flags={},
        leg_fatigue="unknown",
        soreness="unknown",
        schedule_changed=None,
        equipment_changed=None,
        extra_unknowns=[],
    )
    return build_window(request, NOW)


def _fit(
    *,
    sport: int = 1,
    sub_sport: int | None = None,
    seconds: int = 45 * 60,
    metres: int | None = 8400,
    started: dt.datetime = dt.datetime(2026, 8, 9, 6, 0, tzinfo=dt.timezone.utc),
    big_endian: bool = False,
    developer_fields: bool = False,
    local_offset_hours: int | None = None,
) -> bytes:
    """One synthetic FIT file carrying a file_id and one session message.

    Written to the format's own rules rather than to the decoder's expectations: a
    12-byte header, a definition message per local type, then data messages whose field
    widths come from the definition. The architecture byte is honoured, so ``big_endian``
    exercises the branch a Garmin file would never show but the format allows.
    """
    order = ">" if big_endian else "<"
    architecture = 1 if big_endian else 0
    epoch = dt.datetime(1989, 12, 31, tzinfo=dt.timezone.utc)
    stamp = int((started - epoch).total_seconds())

    body = b""
    # file_id (global 0) as local type 0: serial_number (uint32z), time_created (uint32).
    body += bytes([0x40, 0x00, architecture]) + struct.pack(f"{order}H", 0) + bytes([2])
    body += bytes([3, 4, 0x8C]) + bytes([4, 4, 0x86])
    body += bytes([0x00]) + struct.pack(f"{order}I", 3141592) + struct.pack(f"{order}I", stamp)

    # session (global 18) as local type 1.
    fields: list[tuple[int, int, int]] = [
        (2, 4, 0x86),  # start_time
        (5, 1, 0x00),  # sport
        (6, 1, 0x00),  # sub_sport
        (8, 4, 0x86),  # total_timer_time, milliseconds
        (9, 4, 0x86),  # total_distance, centimetres
    ]
    header_byte = 0x41 | (0x20 if developer_fields else 0x00)
    body += bytes([header_byte, 0x00, architecture]) + struct.pack(f"{order}H", 18)
    body += bytes([len(fields)])
    for number, size, base in fields:
        body += bytes([number, size, base])
    if developer_fields:
        # One application-defined field the decoder must skip by width alone, or every
        # message after it reads at the wrong offset.
        body += bytes([1]) + bytes([0, 2, 0])
    body += bytes([0x01])
    body += struct.pack(f"{order}I", stamp)
    body += bytes([sport])
    body += bytes([0xFF if sub_sport is None else sub_sport])
    body += struct.pack(f"{order}I", seconds * 1000)
    body += struct.pack(f"{order}I", 0xFFFFFFFF if metres is None else metres * 100)
    if developer_fields:
        body += struct.pack(f"{order}H", 1234)

    if local_offset_hours is not None:
        # The `activity` message (global 34), carrying the one thing in a FIT file that
        # is not UTC: timestamp and local_timestamp, whose difference is the device's own
        # offset. A real Garmin file always writes it.
        body += bytes([0x42, 0x00, architecture]) + struct.pack(f"{order}H", 34) + bytes([2])
        body += bytes([253, 4, 0x86]) + bytes([5, 4, 0x86])
        body += bytes([0x02])
        body += struct.pack(f"{order}I", stamp)
        body += struct.pack(f"{order}I", stamp + local_offset_hours * 3600)

    header = struct.pack("<BBHI4s", 12, 0x20, 2178, len(body), b".FIT")
    return header + body + b"\x00\x00"


# --------------------------------------------------------------------------------------
# CSV
# --------------------------------------------------------------------------------------


class CsvReadingTests(unittest.TestCase):
    def test_a_recognised_export_needs_no_mapping_and_states_its_own_units(self):
        reading = read_payload(format_name="csv", content=STRAVA_CSV)

        self.assertEqual("strava", reading["recognised_as"])
        self.assertEqual(
            [("2026-08-10", "running", 45, 8.1), ("2026-08-10", "running", 30, 4.0),
             ("2026-08-11", "swimming", 40, 1.2)],
            [
                (row["date"], row["sport"], row["duration_minutes"], row["distance_km"])
                for row in reading["activities"]
            ],
        )
        # Elapsed Time is seconds and Distance is kilometres in this export, and neither
        # was asked of the caller: that is the whole value of recognising the header.
        self.assertEqual([], reading["unreadable"])

    def test_the_start_time_the_export_carries_survives_as_the_row_identity(self):
        reading = read_payload(format_name="csv", content=STRAVA_CSV)

        # Two runs on one day, told apart by the only thing that can tell them apart.
        self.assertEqual(
            ["2026-08-10 06:12:00", "2026-08-10 18:30:00", "2026-08-11 07:00:00"],
            [row["started_at"] for row in reading["activities"]],
        )

    def test_an_intervals_export_reads_metres_and_seconds(self):
        content = (
            "id,start_date_local,type,moving_time,distance,name\n"
            "i7788,2026-08-12T07:15:00,Ride,3600,32500,Long ride\n"
        )

        reading = read_payload(format_name="csv", content=content)

        self.assertEqual("intervals_icu", reading["recognised_as"])
        row = reading["activities"][0]
        self.assertEqual(("2026-08-12", "cycling", 60, 32.5, "i7788"),
                         (row["date"], row["sport"], row["duration_minutes"],
                          row["distance_km"], row["external_id"]))

    def test_a_garmin_export_imports_the_session_and_refuses_to_guess_the_distance_unit(self):
        """The one export that states no unit, and the row still lands.

        Garmin Connect writes a bare `Distance` column whose unit follows the account.
        Reading it as kilometres would put a mile-unit athlete's year of running 60%
        short, and the coach would never see it -- so the field is dropped with a named
        reason and the session imports without it (AGENTS.md 3).
        """
        content = (
            "Activity Type,Date,Favorite,Title,Distance,Time\n"
            "Running,2026-08-14 06:30:00,false,晨跑,5.20,00:28:41\n"
        )

        reading = read_payload(format_name="csv", content=content)

        self.assertEqual("garmin_connect", reading["recognised_as"])
        row = reading["activities"][0]
        self.assertEqual(("2026-08-14", "running", 29), (row["date"], row["sport"],
                                                         row["duration_minutes"]))
        self.assertIsNone(row["distance_km"])
        self.assertEqual(1, len(row["dropped"]))
        self.assertIn("distance_unit", row["dropped"][0])

    def test_an_unrecognised_header_asks_for_a_mapping_and_names_its_own_columns(self):
        content = "When,What,HowLong\n2026-08-14,Run,45\n"

        with self.assertRaises(EvidenceImportError) as caught:
            read_payload(format_name="csv", content=content)

        # The message has to be actionable by the model that holds the file: it names
        # both what is missing and what the header actually said.
        self.assertIn("column_mapping", str(caught.exception))
        self.assertIn("When, What, HowLong", str(caught.exception))

    def test_a_mapping_reads_an_export_nothing_recognises(self):
        content = "When,What,HowLong,HowFar\n2026-08-14,Run,45,5.2\n"

        reading = read_payload(
            format_name="csv",
            content=content,
            column_mapping={
                "date": "When",
                "sport": "What",
                "duration": "HowLong",
                "duration_unit": "minutes",
                "distance": "HowFar",
                "distance_unit": "mi",
            },
        )

        row = reading["activities"][0]
        self.assertIsNone(reading["recognised_as"])
        self.assertEqual(("2026-08-14", "running", 45), (row["date"], row["sport"],
                                                         row["duration_minutes"]))
        self.assertEqual(8.369, row["distance_km"])

    def test_a_mapping_that_names_a_distance_without_its_unit_is_refused(self):
        content = "When,What,HowLong,HowFar\n2026-08-14,Run,45,5.2\n"

        with self.assertRaises(EvidenceImportError) as caught:
            read_payload(
                format_name="csv",
                content=content,
                column_mapping={
                    "date": "When", "sport": "What",
                    "duration": "HowLong", "duration_unit": "minutes",
                    "distance": "HowFar",
                },
            )

        self.assertIn("distance_unit", str(caught.exception))

    def test_a_mapping_naming_a_column_the_file_lacks_is_refused_by_name(self):
        with self.assertRaises(EvidenceImportError) as caught:
            read_payload(
                format_name="csv",
                content="When,What,HowLong\n2026-08-14,Run,45\n",
                column_mapping={
                    "date": "When", "sport": "Sport",
                    "duration": "HowLong", "duration_unit": "minutes",
                },
            )

        self.assertIn("Sport", str(caught.exception))

    def test_one_unreadable_row_costs_only_that_row(self):
        content = (
            "Activity ID,Activity Date,Activity Type,Elapsed Time,Distance\n"
            "1,2026-08-10 06:00:00,Run,2700,8.1\n"
            "2,not a date,Run,2700,8.1\n"
            "3,2026-08-11 06:00:00,Padel,2700,\n"
            "4,2026-08-12 06:00:00,Run,,8.1\n"
            "5,2026-08-13 06:00:00,Run,2700,8.1\n"
        )

        reading = read_payload(format_name="csv", content=content)

        self.assertEqual(2, len(reading["activities"]))
        # Each refusal names its row number and what the file actually said, so the
        # athlete can see which session is missing rather than a count.
        self.assertEqual(
            [(3, "not a date"), (4, "Padel"), (5, None)],
            [(row["row"], row["source_value"]) for row in reading["unreadable"]],
        )

    def test_hh_mm_ss_and_mm_ss_are_both_durations(self):
        content = "When,What,HowLong\n2026-08-14,Run,01:02:30\n2026-08-15,Run,28:41\n"

        reading = read_payload(
            format_name="csv",
            content=content,
            column_mapping={"date": "When", "sport": "What",
                            "duration": "HowLong", "duration_unit": "hh:mm:ss"},
        )

        self.assertEqual([63, 29], [row["duration_minutes"] for row in reading["activities"]])


# --------------------------------------------------------------------------------------
# Apple Health
# --------------------------------------------------------------------------------------


APPLE_XML = """<?xml version="1.0" encoding="UTF-8"?>
<HealthData locale="zh_TW">
 <Record type="HKQuantityTypeIdentifierBodyMass" unit="kg" startDate="2026-08-10 07:01:00 +0800" value="72.5"/>
 <Record type="HKQuantityTypeIdentifierBodyMass" unit="kg" startDate="2026-08-10 21:01:00 +0800" value="72.9"/>
 <Record type="HKQuantityTypeIdentifierHeartRate" unit="count/min" startDate="2026-08-10 07:01:00 +0800" value="58"/>
 <Record type="HKQuantityTypeIdentifierBodyFatPercentage" unit="%" startDate="2026-08-10 07:01:00 +0800" value="0.184"/>
 <Workout workoutActivityType="HKWorkoutActivityTypeRunning" duration="42.5" durationUnit="min" totalDistance="8.4" totalDistanceUnit="km" startDate="2026-08-09 06:00:00 +0800"/>
 <Record type="HKQuantityTypeIdentifierRestingHeartRate" unit="count/min" startDate="2026-08-10 05:30:00 +0800" value="47"/>
 <Record type="HKQuantityTypeIdentifierHeartRateVariabilitySDNN" unit="ms" startDate="2026-08-10 05:30:00 +0800" value="63"/>
 <Record type="HKCategoryTypeIdentifierSleepAnalysis" startDate="2026-08-10 00:10:00 +0800" value="HKCategoryValueSleepAnalysisAsleepDeep"/>
 <Workout workoutActivityType="HKWorkoutActivityTypePickleball" duration="30" durationUnit="min" startDate="2026-08-09 18:00:00 +0800"/>
</HealthData>"""


class AppleHealthRecoveryReadingTests(unittest.TestCase):
    """Which recovery readings cross out of an export, and what is said about the rest."""

    def test_resting_heart_rate_crosses_and_carries_its_day(self):
        reading = read_payload(format_name="apple_health_xml", content=APPLE_XML)
        self.assertEqual(
            [{"date": "2026-08-10", "resting_hr_bpm": 47.0}], reading["recovery"]
        )

    def test_hrv_and_sleep_are_declined_by_name_rather_than_dropped(self):
        # HRV is the one that matters: Apple records SDNN and the provider reports RMSSD,
        # so storing Apple's figure under the same name would put two different
        # measurements of one night into one series. Sleep is declined for its own reason
        # -- per-stage intervals are a night to be assembled, and nothing here assembles
        # evidence. Both are named, because an athlete uploading a health export believes
        # they just handed over exactly these two.
        reading = read_payload(format_name="apple_health_xml", content=APPLE_XML)
        declined = " ".join(reading["ignored"])
        self.assertIn("SDNN", declined)
        self.assertIn("RMSSD", declined)
        self.assertIn("sleep", declined)
        self.assertEqual([], [row for row in reading["recovery"] if "hrv_last_night_ms" in row])

    def test_readings_this_coach_does_not_keep_are_counted_under_one_heading(self):
        # Per-beat heart rate, steps, dietary anything. Counted rather than named: an
        # export carries hundreds of thousands of them and naming each would bury the
        # sessions the athlete asked about.
        reading = read_payload(format_name="apple_health_xml", content=APPLE_XML)
        self.assertEqual(1, reading["ignored"]["other readings this coach does not keep"])


class AppleHealthReadingTests(unittest.TestCase):
    def test_workouts_and_body_measurements_come_out_of_one_export(self):
        reading = read_payload(format_name="apple_health_xml", content=APPLE_XML)

        self.assertEqual(
            # 42.5 minutes, rounded half-up the way a person reads a duration.
            [("2026-08-09", "running", 43, 8.4)],
            [(row["date"], row["sport"], row["duration_minutes"], row["distance_km"])
             for row in reading["activities"]],
        )
        # Heart rate is not a measurement this product holds and is not an error either:
        # reporting every non-body Record would bury one real problem under a hundred
        # thousand non-problems.
        self.assertEqual(
            [("weight_kg", 72.5), ("weight_kg", 72.9), ("body_fat_pct", 18.4)],
            [(row["field"], row["value"]) for row in reading["measurements"]],
        )
        self.assertEqual(
            ["HKWorkoutActivityTypePickleball"],
            [row["source_value"] for row in reading["unreadable"]],
        )

    def test_a_fragment_reads_exactly_as_the_whole_file_does(self):
        """The only way a 400 MB export can reach a tool call at all.

        The reader takes elements, not a document, so the caller may pass whatever subset
        it managed to filter out -- and gets the identical rows for the elements it sent.
        """
        fragment = "\n".join(line for line in APPLE_XML.splitlines() if "<Workout" in line)

        whole = read_payload(format_name="apple_health_xml", content=APPLE_XML)
        part = read_payload(format_name="apple_health_xml", content=fragment)

        # Everything except where in the payload the element sat, which is the one thing
        # a fragment genuinely changes and the one thing nothing downstream reads.
        def without_position(reading):
            return [
                {key: value for key, value in row.items() if key != "row"}
                for row in reading["activities"]
            ]

        self.assertEqual(without_position(whole), without_position(part))

    def test_pounds_are_converted_and_a_percentage_written_as_a_fraction_is_read_as_one(self):
        content = (
            '<Record type="HKQuantityTypeIdentifierBodyMass" unit="lb" '
            'startDate="2026-08-10" value="160"/>'
        )

        reading = read_payload(format_name="apple_health_xml", content=content)

        self.assertEqual(72.57, reading["measurements"][0]["value"])

    def test_xml_holding_no_elements_this_reads_says_so_rather_than_importing_nothing(self):
        with self.assertRaises(EvidenceImportError) as caught:
            read_payload(format_name="apple_health_xml", content="<HealthData/>")

        self.assertIn("<Workout>", str(caught.exception))


# --------------------------------------------------------------------------------------
# FIT
# --------------------------------------------------------------------------------------


class FitReadingTests(unittest.TestCase):
    def test_one_session_is_decoded_out_of_the_binary(self):
        reading = read_payload(format_name="fit", content=base64.b64encode(_fit()).decode())

        row = reading["activities"][0]
        self.assertEqual(("2026-08-09", "running", 45, 8.4),
                         (row["date"], row["sport"], row["duration_minutes"],
                          row["distance_km"]))
        # file_id's serial and creation time give a re-upload of the same file the same
        # identity, which is what makes a second import a skip rather than a duplicate.
        self.assertEqual("fit-3141592-1155189600-1", row["external_id"])

    def test_a_big_endian_file_reads_identically(self):
        little = read_payload(format_name="fit", content=base64.b64encode(_fit()).decode())
        big = read_payload(
            format_name="fit", content=base64.b64encode(_fit(big_endian=True)).decode()
        )

        self.assertEqual(little["activities"], big["activities"])

    def test_developer_fields_are_skipped_by_width_without_derailing_the_stream(self):
        reading = read_payload(
            format_name="fit",
            content=base64.b64encode(_fit(developer_fields=True)).decode(),
        )

        self.assertEqual(45, reading["activities"][0]["duration_minutes"])

    def test_strength_is_the_training_sport_with_its_own_sub_sport(self):
        reading = read_payload(
            format_name="fit",
            content=base64.b64encode(_fit(sport=10, sub_sport=20, metres=None)).decode(),
        )

        row = reading["activities"][0]
        self.assertEqual("strength", row["sport"])
        self.assertIsNone(row["distance_km"])

    def test_the_day_comes_from_the_devices_own_offset_not_from_utc(self):
        """The bug a real Garmin file found: a 6 a.m. run in Taipei is UTC the day before.

        Everything in a FIT file is UTC except `activity.local_timestamp`, so a session
        dated off the raw timestamp lands a day early for exactly the sessions a
        UTC+8 athlete does most. The offset the device recorded is the answer, and it
        beats anything this product could assume about where they were that morning.
        """
        payload = _fit(
            started=dt.datetime(2026, 8, 9, 22, 12, tzinfo=dt.timezone.utc),
            local_offset_hours=8,
        )

        reading = read_payload(format_name="fit", content=base64.b64encode(payload).decode())

        row = reading["activities"][0]
        self.assertEqual("2026-08-10", row["date"])
        self.assertEqual("2026-08-10T06:12:00+08:00", row["started_at"])

    def test_without_an_offset_the_athletes_own_stored_timezone_answers(self):
        """A fallback to a fact this product already holds, never to an assumption."""
        payload = _fit(started=dt.datetime(2026, 8, 9, 22, 12, tzinfo=dt.timezone.utc))

        reading = read_payload(
            format_name="fit",
            content=base64.b64encode(payload).decode(),
            timezone_name="Asia/Taipei",
        )

        self.assertEqual("2026-08-10", reading["activities"][0]["date"])

    def test_a_sport_this_product_does_not_hold_is_named_rather_than_resolved(self):
        reading = read_payload(
            format_name="fit", content=base64.b64encode(_fit(sport=8)).decode()
        )

        self.assertEqual([], reading["activities"])
        self.assertEqual(1, len(reading["unreadable"]))

    def test_something_that_is_not_a_fit_file_is_refused_before_anything_is_read(self):
        with self.assertRaises(EvidenceImportError):
            read_payload(
                format_name="fit", content=base64.b64encode(b"not a fit file at all").decode()
            )

    def test_content_that_is_not_base64_is_refused_by_name(self):
        with self.assertRaises(EvidenceImportError) as caught:
            read_payload(format_name="fit", content="這不是 base64")

        self.assertIn("base64", str(caught.exception))

    def test_a_truncated_file_is_refused_rather_than_half_read(self):
        payload = _fit()
        with self.assertRaises(EvidenceImportError):
            read_payload(
                format_name="fit", content=base64.b64encode(payload[: len(payload) // 2]).decode()
            )


# --------------------------------------------------------------------------------------
# Rows the caller read
# --------------------------------------------------------------------------------------


class RecordReadingTests(unittest.TestCase):
    def test_rows_the_caller_supplies_normalize_exactly_as_a_parsed_row_does(self):
        reading = read_payload(
            format_name="records",
            records=[{"date": "2026-08-14", "sport": "Trail Running", "duration_minutes": 95,
                      "distance_km": 14.2, "note": "陽明山"}],
        )

        row = reading["activities"][0]
        self.assertEqual(("2026-08-14", "running", 95, 14.2, "陽明山"),
                         (row["date"], row["sport"], row["duration_minutes"],
                          row["distance_km"], row["note"]))

    def test_a_record_carrying_a_field_this_does_not_know_is_reported_not_ignored(self):
        reading = read_payload(
            format_name="records",
            records=[{"date": "2026-08-14", "sport": "Run", "duration_minutes": 45,
                      "average_hr": 148}],
        )

        self.assertEqual([], reading["activities"])
        self.assertIn("average_hr", reading["unreadable"][0]["reason"])

    def test_content_and_records_are_not_interchangeable(self):
        with self.assertRaises(EvidenceImportError):
            read_payload(format_name="records", content=STRAVA_CSV)
        with self.assertRaises(EvidenceImportError):
            read_payload(format_name="csv", records=[{"date": "2026-08-14"}])

    def test_an_unknown_format_is_refused_with_the_list_of_real_ones(self):
        with self.assertRaises(EvidenceImportError) as caught:
            read_payload(format_name="tcx", content="<x/>")

        self.assertIn("csv", str(caught.exception))


# --------------------------------------------------------------------------------------
# Sameness: the one rule the dedup is built on
# --------------------------------------------------------------------------------------


class SamenessTests(unittest.TestCase):
    def _session(self, **overrides):
        return {"date": "2026-08-10", "sport": "running", "duration_minutes": 45,
                "distance_km": 8.1, **overrides}

    def test_a_few_minutes_apart_is_one_session_described_twice(self):
        self.assertTrue(
            same_reported_session(self._session(), self._session(duration_minutes=43))
        )

    def test_a_different_day_or_sport_is_never_the_same_session(self):
        self.assertFalse(
            same_reported_session(self._session(), self._session(date="2026-08-11"))
        )
        self.assertFalse(
            same_reported_session(self._session(), self._session(sport="cycling"))
        )

    def test_the_same_duration_over_a_very_different_distance_is_two_sessions(self):
        self.assertFalse(
            same_reported_session(self._session(), self._session(distance_km=15.0))
        )

    def test_a_distance_only_one_side_states_decides_nothing(self):
        """An export with no unit and an athlete who said only "45 分鐘" both leave it null.

        Neither absence is evidence of a second session, so the duration alone answers.
        """
        self.assertTrue(
            same_reported_session(self._session(distance_km=None), self._session())
        )


# --------------------------------------------------------------------------------------
# Writing: added, merged, skipped, and the one question
# --------------------------------------------------------------------------------------


class ImportWritingTests(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.state_dir = Path(self._dir.name)

    def _import(self, content=STRAVA_CSV, *, format_name="csv", resolutions=None,
                source_name=None, **kwargs):
        reading = read_payload(format_name=format_name, content=content, **kwargs)
        return import_reported_evidence(
            self.state_dir,
            activities=reading["activities"],
            measurements=reading["measurements"],
            unreadable=reading["unreadable"],
            format_name=reading["format"],
            recognised_as=reading["recognised_as"],
            digest=reading["digest"],
            source_name=source_name,
            resolutions=resolutions,
            now=NOW,
        )

    def _stored(self):
        return load_evidence(self.state_dir)["reported_activities"]

    def test_every_session_lands_and_carries_where_it_came_from(self):
        result = self._import(source_name="Strava export")

        self.assertEqual(3, result["counts"]["added"])
        stored = self._stored()
        self.assertEqual(3, len(stored))
        for record in stored:
            self.assertEqual(ATHLETE_IMPORTED_SOURCE, record["source"])
            self.assertEqual("Strava export", record["import"]["source_name"])
            self.assertEqual("strava", record["import"]["recognised_as"])

    def test_two_sessions_of_one_sport_on_one_day_are_both_kept(self):
        """A conversation could never tell these apart. A file already did.

        The spoken path holds one summary per sport per day precisely because "40 分鐘，
        啊是 45" is one session stated twice. An export carries start times, so a morning
        run and an evening run are two rows that are provably not one row restated -- and
        asking the athlete which of them is real would be asking them to re-read the file
        they just handed over.
        """
        self._import()

        runs = [row for row in self._stored() if row["sport"] == "running"]
        self.assertEqual(2, len(runs))
        self.assertEqual([45, 30], sorted((row["duration_minutes"] for row in runs),
                                          reverse=True))

    def test_the_same_upload_a_second_time_writes_nothing(self):
        self._import()

        again = self._import()

        self.assertTrue(again["already_imported"])
        self.assertEqual(3, len(self._stored()))
        self.assertEqual([], again["added"])

    def test_a_different_export_holding_the_same_sessions_skips_them(self):
        """The re-upload that is not byte-identical: a wider export of the same account.

        The digest does not recognise it, so every row is looked at -- and every row is
        already on record under the same source identity, which is what the per-session
        key is for.
        """
        self._import()

        wider = STRAVA_CSV + "9004,2026-08-13 07:00:00,Recovery,Run,1500,3.0\n"
        result = self._import(wider)

        self.assertEqual(1, result["counts"]["added"])
        self.assertEqual(3, result["counts"]["skipped"])
        self.assertEqual(4, len(self._stored()))

    def test_a_session_already_on_record_under_another_id_merges_instead_of_doubling(self):
        """Two exports of one account, from two tools, with two id spaces.

        Nothing links the ids, so the numbers have to answer: same day, same sport, same
        duration is one session. The standing record is left exactly as it is and only
        gains the second export's key, which is what makes the *next* re-import a skip.
        """
        self._import()

        other_tool = (
            "id,start_date_local,type,moving_time,distance,name\n"
            "z1,2026-08-11T07:00:00,Swim,2410,1180,Pool\n"
        )
        result = self._import(other_tool)

        self.assertEqual(0, result["counts"]["added"])
        self.assertEqual(1, result["counts"]["merged"])
        self.assertEqual(3, len(self._stored()))
        # And a third pass changes nothing at all -- recognised by the upload's own
        # digest before a single row is looked at.
        self.assertTrue(self._import(other_tool)["already_imported"])
        self.assertEqual(3, len(self._stored()))

    def test_a_file_that_already_told_the_two_apart_does_not_ask_about_them(self):
        """The false question: an upload accounting for a record, then being asked about it.

        The athlete said "跑了 45 分鐘" for a Monday. Their Strava export holds that run
        plus an evening one. The morning row merges into what they said; the evening row
        used to find that record still standing and unaccounted for, and asked -- which is
        asking them to re-read the file they just handed over. It *was* accounted for, by
        the row immediately before it, out of the same file.

        The control below is the case that still asks, and must: one row, disagreeing,
        with nothing in the upload to explain the difference.
        """
        spoken = record_activity_summary(
            self.state_dir, sport="running", duration_minutes=45, date="2026-08-11", now=NOW
        )
        self.assertEqual(1, spoken["activity_count"])

        result = self._import(
            "Activity ID,Activity Date,Activity Name,Activity Type,Elapsed Time,Distance\n"
            "1,2026-08-11 06:12:00,Morning,Run,2700,8.1\n"
            "2,2026-08-11 18:30:00,Evening,Run,1800,4.0\n"
        )

        self.assertEqual(0, result["counts"]["needs_confirmation"])
        self.assertEqual(1, result["counts"]["merged"])
        self.assertEqual(1, result["counts"]["added"])
        self.assertEqual(2, len(self._stored()))

    def test_one_disagreeing_row_with_nothing_to_explain_it_still_asks(self):
        """The control for the test above: this is what a real ambiguity looks like."""
        record_activity_summary(
            self.state_dir, sport="running", duration_minutes=45, date="2026-08-11", now=NOW
        )

        result = self._import(
            "Activity ID,Activity Date,Activity Name,Activity Type,Elapsed Time,Distance\n"
            "2,2026-08-11 18:30:00,Evening,Run,1800,4.0\n"
        )

        self.assertEqual(1, result["counts"]["needs_confirmation"])
        self.assertEqual(0, result["counts"]["added"])
        self.assertEqual(1, len(self._stored()))

    def test_a_disagreeing_session_is_asked_about_and_nothing_is_written(self):
        self._import()

        conflicting = (
            "id,start_date_local,type,moving_time,distance,name\n"
            "z9,2026-08-11T07:00:00,Swim,3600,1600,Pool\n"
        )
        result = self._import(conflicting)

        self.assertEqual(1, result["counts"]["needs_confirmation"])
        self.assertEqual(0, result["counts"]["added"])
        self.assertEqual(3, len(self._stored()))
        question = result["needs_confirmation"][0]
        self.assertEqual(60, question["incoming"]["duration_minutes"])
        self.assertEqual([40], [row["duration_minutes"] for row in question["on_record"]])

    def test_an_upload_that_ended_in_questions_is_not_recorded_as_finished(self):
        """Or the call carrying the answers would be read as a repeat of itself.

        This is the failure the digest ledger would otherwise cause: recording the upload
        before the athlete answered would make the answering call a no-op, and the rows it
        was meant to write would never land.
        """
        self._import()
        conflicting = (
            "id,start_date_local,type,moving_time,distance,name\n"
            "z9,2026-08-11T07:00:00,Swim,3600,1600,Pool\n"
        )
        first = self._import(conflicting)

        answered = self._import(
            conflicting,
            resolutions=[{"conflict_id": first["needs_confirmation"][0]["conflict_id"],
                          "resolution": "separate_session"}],
        )

        self.assertFalse(answered["already_imported"])
        self.assertEqual(1, answered["counts"]["added"])
        self.assertEqual(4, len(self._stored()))

    def test_the_same_session_answer_merges_it_into_what_stands(self):
        self._import()
        conflicting = (
            "id,start_date_local,type,moving_time,distance,name\n"
            "z9,2026-08-11T07:00:00,Swim,3600,1600,Pool\n"
        )
        first = self._import(conflicting)

        answered = self._import(
            conflicting,
            resolutions=[{"conflict_id": first["needs_confirmation"][0]["conflict_id"],
                          "resolution": "same_session"}],
        )

        self.assertEqual(1, answered["counts"]["merged"])
        self.assertEqual(3, len(self._stored()))
        # Merging changed no stated number: the record that stands is the one that stood.
        swim = next(row for row in self._stored() if row["sport"] == "swimming")
        self.assertEqual(40, swim["duration_minutes"])

    def test_a_conflict_id_is_the_same_one_the_next_call_can_answer(self):
        self._import()
        conflicting = (
            "id,start_date_local,type,moving_time,distance,name\n"
            "z9,2026-08-11T07:00:00,Swim,3600,1600,Pool\n"
        )

        first = self._import(conflicting)
        second = self._import(conflicting)

        self.assertEqual(first["needs_confirmation"][0]["conflict_id"],
                         second["needs_confirmation"][0]["conflict_id"])

    def test_a_resolution_naming_an_answer_this_does_not_take_is_refused(self):
        with self.assertRaises(AthleteEvidenceError):
            self._import(resolutions=[{"conflict_id": "abc", "resolution": "probably"}])

    def test_the_rows_it_could_not_read_travel_with_the_report(self):
        content = (
            "Activity ID,Activity Date,Activity Type,Elapsed Time,Distance\n"
            "1,2026-08-10 06:00:00,Padel,2700,8.1\n"
        )

        result = self._import(content)

        self.assertEqual(1, result["counts"]["unreadable"])
        self.assertEqual("Padel", result["unreadable"][0]["source_value"])


class ImportedMeasurementTests(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.state_dir = Path(self._dir.name)

    def _import(self, content=APPLE_XML):
        reading = read_payload(format_name="apple_health_xml", content=content)
        return import_reported_evidence(
            self.state_dir,
            activities=reading["activities"],
            measurements=reading["measurements"],
            unreadable=reading["unreadable"],
            format_name=reading["format"],
            recognised_as=reading["recognised_as"],
            digest=reading["digest"],
            now=NOW,
        )

    def test_one_record_a_day_and_the_last_reading_of_that_day_is_the_one_kept(self):
        result = self._import()

        stored = load_evidence(self.state_dir)["body_measurements"]
        self.assertEqual(1, len(stored))
        self.assertEqual(72.9, stored[0]["weight_kg"])
        self.assertEqual(18.4, stored[0]["body_fat_pct"])
        self.assertEqual(ATHLETE_IMPORTED_SOURCE, stored[0]["source"])
        self.assertEqual(1, result["counts"]["measurements_added"])

    def test_a_day_the_athlete_already_stated_is_left_exactly_as_they_stated_it(self):
        """An export from last year is not a newer statement than yesterday's sentence.

        The module's rule read from the other end: the newest *statement* wins, and an
        upload is not a statement about a day the athlete has already spoken about.
        """
        from garmin_coach_loop.athlete_evidence import record_body_measurement

        record_body_measurement(self.state_dir, date="2026-08-10", weight_kg=71.0, now=NOW)

        result = self._import()

        stored = load_evidence(self.state_dir)["body_measurements"]
        self.assertEqual([71.0], [row["weight_kg"] for row in stored])
        self.assertEqual(1, result["counts"]["measurements_skipped"])

    def test_an_imported_measurement_reads_back_as_imported_not_as_stated(self):
        """The same provenance rule the sessions follow, on the other group.

        A number read off a scale this morning and one an export supplied are both the
        athlete's word, and a coach reading a weight series should be able to see which
        days are which.
        """
        from garmin_coach_loop.athlete_evidence import body_measurement_series

        self._import()

        series = body_measurement_series(load_evidence(self.state_dir), _window())

        self.assertEqual([ATHLETE_IMPORTED_SOURCE], [row["source"] for row in series])

    def test_an_imported_measurement_is_retractable_like_any_other(self):
        from garmin_coach_loop.athlete_evidence import retract_body_measurement

        self._import()

        result = retract_body_measurement(self.state_dir, date="2026-08-10", now=NOW)

        self.assertTrue(result["retracted"])
        self.assertEqual(72.9, result["removed"]["weight_kg"])
        self.assertEqual([], load_evidence(self.state_dir)["body_measurements"])

    def test_a_figure_outside_the_bounds_is_refused_through_this_door_too(self):
        content = (
            '<Record type="HKQuantityTypeIdentifierBodyMass" unit="kg" '
            'startDate="2026-08-10" value="7.23"/>'
        )

        result = self._import(content)

        self.assertEqual(0, result["counts"]["measurements_added"])
        self.assertEqual([], load_evidence(self.state_dir)["body_measurements"])


# --------------------------------------------------------------------------------------
# Where an upload meets what the athlete says
# --------------------------------------------------------------------------------------


class SpokenAndImportedTests(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.state_dir = Path(self._dir.name)
        reading = read_payload(format_name="csv", content=STRAVA_CSV)
        import_reported_evidence(
            self.state_dir,
            activities=reading["activities"],
            measurements=[],
            unreadable=[],
            format_name="csv",
            recognised_as="strava",
            digest=reading["digest"],
            source_name="Strava export",
            now=NOW,
        )

    def test_stating_a_session_an_upload_already_holds_corrects_it_rather_than_doubling_it(self):
        """The double count the other way round, and the same rule answers it.

        "那天早上跑 44 分鐘" is the athlete describing a session their Strava export already
        described. Storing it beside the imported row would put one session in the coach's
        context twice -- so it takes over that record, and the upload's own key travels
        with it so re-importing the file still recognises the session.
        """
        result = record_activity_summary(
            self.state_dir, sport="running", duration_minutes=44, date="2026-08-10",
            subjective_feel=3, now=NOW,
        )

        self.assertEqual(3, result["activity_count"])
        self.assertEqual(ATHLETE_REPORTED_SOURCE, result["activity"]["source"])
        self.assertEqual("Strava export", result["activity"]["import"]["source_name"])
        self.assertTrue(result["activity"]["dedup_keys"])
        self.assertIn("upload", result["replaced_note"])

    def test_a_session_the_upload_does_not_hold_is_a_new_record(self):
        result = record_activity_summary(
            self.state_dir, sport="mobility", duration_minutes=20, date="2026-08-10", now=NOW
        )

        self.assertEqual(4, result["activity_count"])
        self.assertIsNone(result["activity"]["import"])

    def test_retraction_refuses_to_pick_for_the_athlete_when_a_day_holds_two(self):
        result = retract_activity_summary(
            self.state_dir, sport="running", date="2026-08-10", now=NOW
        )

        self.assertFalse(result["retracted"])
        self.assertIsNone(result["removed"])
        self.assertEqual(
            ["2026-08-10 06:12:00", "2026-08-10 18:30:00"],
            sorted(row["started_at"] for row in result["candidates"]),
        )
        self.assertEqual(3, len(load_evidence(self.state_dir)["reported_activities"]))

    def test_the_start_time_the_candidates_offered_is_what_removes_one(self):
        result = retract_activity_summary(
            self.state_dir, sport="running", date="2026-08-10",
            started_at="2026-08-10 18:30:00", now=NOW,
        )

        self.assertTrue(result["retracted"])
        self.assertEqual(30, result["removed"]["duration_minutes"])
        self.assertEqual(2, len(load_evidence(self.state_dir)["reported_activities"]))

    def test_a_day_holding_one_session_still_retracts_without_a_start_time(self):
        result = retract_activity_summary(
            self.state_dir, sport="swimming", date="2026-08-11", now=NOW
        )

        self.assertTrue(result["retracted"])
        self.assertEqual(40, result["removed"]["duration_minutes"])

    def test_imported_sessions_reach_the_coach_labelled_and_beside_nothing_else(self):
        summaries = reported_activity_summaries(load_evidence(self.state_dir), _window())

        self.assertEqual(3, len(summaries))
        for row in summaries:
            self.assertEqual(ATHLETE_IMPORTED_SOURCE, row["source"])
            self.assertEqual("Strava export", row["imported_from"])
            # The absent keys are still the contract: nothing an attachment could be
            # built from arrived with the file.
            self.assertNotIn("activity_id", row)
            self.assertNotIn("match_confidence", row)

    def test_history_older_than_the_window_is_stored_and_simply_not_in_this_context(self):
        old = (
            "id,start_date_local,type,moving_time,distance,name\n"
            "old1,2019-04-02T07:00:00,Run,3600,10000,Old race\n"
        )
        reading = read_payload(format_name="csv", content=old)
        import_reported_evidence(
            self.state_dir, activities=reading["activities"], measurements=[], unreadable=[],
            format_name="csv", recognised_as="intervals_icu", digest=reading["digest"], now=NOW,
        )

        stored = load_evidence(self.state_dir)["reported_activities"]
        summaries = reported_activity_summaries(load_evidence(self.state_dir), _window())

        self.assertEqual(4, len(stored))
        self.assertEqual(3, len(summaries))


if __name__ == "__main__":
    unittest.main()
