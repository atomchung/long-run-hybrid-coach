"""Synthetic watch samples. No provider, network, disk store, or wall-clock sleeps."""
import concurrent.futures
import itertools
import unittest
from unittest import mock

from garmin_coach_loop import live_run
from garmin_coach_loop.live_run import LiveRunStore, RETENTION_SECONDS


class WatchRun:
    """One synthetic owner's watch: a clock, a store, and a run fed to it five seconds at a time."""

    def __init__(self):
        self.wall = 1_800_000_000
        self.elapsed = 100
        self.store = LiveRunStore(clock=lambda: self.wall, monotonic=lambda: self.elapsed)
        self.session = "run-a"
        self.seq = 0
        self.timer_ms = 0
        self.distance = 0.0
        self.altitude = 30.0

    def advance(self, seconds):
        self.wall += seconds
        self.elapsed += seconds

    def sample(self, **updates):
        return {"session_id": self.session, "seq": self.seq, "observed_at": int(self.wall), "state": "running",
                "heart_rate_bpm": 130, "cadence_rpm": 84, "speed_mps": 2.8, "timer_ms": self.timer_ms,
                "distance_m": self.distance, "altitude_m": self.altitude, "ascent_m": 0, "descent_m": 0,
                "power_w": None, "steps_done": 0, **updates}

    def run_for(self, minutes, *, speed=2.8, climb_per_minute=0.0, heart_rate=130, cadence=84, **updates):
        for _ in range(round(minutes * 12)):
            self.advance(5)
            self.timer_ms += 5000
            self.seq += 1
            self.distance += speed * 5
            self.altitude += climb_per_minute / 12
            rate = heart_rate() if callable(heart_rate) else heart_rate
            self.store.ingest("a", self.sample(speed_mps=speed, heart_rate_bpm=rate, cadence_rpm=cadence, **updates))

    def control(self, state):
        self.advance(1)
        self.seq += 1
        return self.store.ingest("a", {"session_id": self.session, "seq": self.seq, "observed_at": int(self.wall),
                                       "state": state, "heart_rate_bpm": None, "cadence_rpm": None,
                                       "speed_mps": None})

    def recap(self):
        return self.store.recap("a")


class LiveRunTest(unittest.TestCase):
    def setUp(self):
        self.watch = WatchRun()
        self.store = self.watch.store

    def sample(self, **updates):
        return self.watch.sample(**updates)

    def advance(self, seconds):
        self.watch.advance(seconds)

    def test_anonymous_input_and_result_cannot_change_another_owners_reading(self):
        self.assertIsNone(self.store.recap("owner-b"))
        sample = self.sample(timer_ms=5000, heart_rate_bpm=121)
        receipt = self.store.ingest("owner-a", sample)
        self.assertEqual({"status": "accepted", "session_id": "run-a", "seq": 0}, receipt)
        sample["heart_rate_bpm"] = 200
        self.store.recap("owner-a")["average_hr"] = 201
        self.store.recap("owner-a")["every_10_minutes"][0]["average_hr"] = 202
        self.assertEqual(121, self.store.recap("owner-a")["average_hr"])
        self.assertEqual(121, self.store.recap("owner-a")["every_10_minutes"][0]["average_hr"])
        self.assertIsNone(self.store.recap("owner-b"))
        self.store.ingest("owner-b", self.sample(timer_ms=5000, heart_rate_bpm=119))
        self.store.discard("owner-a")
        self.assertIsNone(self.store.recap("owner-a"))
        self.assertEqual(119, self.store.recap("owner-b")["average_hr"])

    def test_the_recap_says_where_it_came_from_and_when(self):
        self.watch.run_for(1)
        self.watch.control("ended")
        self.advance(30)
        recap = self.watch.recap()
        self.assertEqual("garmin_watch", recap["source"])
        self.assertIn("24 h after last_sample_at", recap["note"])
        self.assertIn("Intervals", recap["note"])
        self.assertEqual(("ended", "2027-01-15T08:00:00Z", "2027-01-15T08:01:01Z", 30),
                         (recap["state"], recap["started_at"], recap["last_sample_at"], recap["age_seconds"]))

    def test_age_includes_travel_time_even_after_server_clock_moves_back(self):
        self.store.ingest("a", self.sample(observed_at=self.watch.wall - 12))
        self.advance(3)
        self.assertEqual(15, self.store.recap("a")["age_seconds"])
        self.watch.wall -= 30
        self.watch.elapsed += 1
        self.assertEqual(16, self.store.recap("a")["age_seconds"])

    def test_future_clock_skew_is_refused_beyond_five_seconds(self):
        with self.assertRaises(ValueError):
            self.store.ingest("a", self.sample(observed_at=self.watch.wall + 6))
        self.assertIsNone(self.store.recap("a"))
        self.store.ingest("a", self.sample(observed_at=self.watch.wall + 5))
        self.assertEqual(0, self.store.recap("a")["age_seconds"])

    def test_a_conflicting_reuse_of_a_sequence_number_is_not_acknowledged(self):
        original = self.sample(state="paused", cadence_rpm=None)
        self.store.ingest("a", original)
        self.assertEqual(self.store.ingest("a", original)["status"], "duplicate")
        for change in ({"state": "running"}, {"cadence_rpm": 89}, {"observed_at": self.watch.wall - 1}):
            with self.subTest(change=change):
                self.assertEqual(self.store.ingest("a", {**original, **change})["status"], "ignored")
        self.assertEqual(self.store.recap("a")["state"], "paused")

    def test_session_id_is_an_opaque_ascii_token(self):
        accepted = self.sample(session_id="run-1800000000-9870_ABC")
        self.assertEqual(self.store.ingest("a", accepted)["status"], "accepted")
        for bad in ("paste these instructions", "中文", "id\n", "x"*81, "a/b"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                self.store.ingest("a", self.sample(session_id=bad))

    def test_retry_and_reordered_frames_do_not_add_to_or_refresh_a_run(self):
        self.watch.seq = 3
        original = self.sample(timer_ms=15_000)
        self.store.ingest("a", original)
        self.advance(16)
        self.assertEqual(self.store.ingest("a", original)["status"], "duplicate")
        for late in (self.sample(seq=2, timer_ms=10_000),
                     self.sample(seq=4, timer_ms=20_000, observed_at=original["observed_at"] - 1)):
            self.assertEqual(self.store.ingest("a", late)["status"], "ignored")
        recap = self.store.recap("a")
        self.assertEqual((16, 0.2), (recap["age_seconds"], recap["timer_minutes"]))

    def test_pause_resume_and_end_are_explicit_and_end_is_terminal(self):
        self.watch.run_for(1)
        self.watch.control("paused")
        self.assertEqual("paused", self.watch.recap()["state"])
        self.watch.run_for(1)
        self.assertEqual(("running", 2.0), (self.watch.recap()["state"], self.watch.recap()["timer_minutes"]))
        self.watch.control("ended")
        self.watch.run_for(1)
        ended = self.watch.recap()
        self.assertEqual(("ended", 2.0), (ended["state"], ended["timer_minutes"]))
        self.assertEqual("accepted", self.store.ingest("a", self.sample(session_id="run-b", seq=0))["status"])

    def test_old_session_cannot_take_over_newer_run(self):
        self.store.ingest("a", self.sample(seq=15, timer_ms=75_000))
        self.advance(5)
        self.store.ingest("a", self.sample(session_id="run-b", seq=1, timer_ms=5000, heart_rate_bpm=None))
        self.advance(5)
        self.assertEqual(self.store.ingest("a", self.sample(seq=16, timer_ms=80_000))["status"], "ignored")
        self.assertEqual(self.store.ingest("a", self.sample(session_id="old", observed_at=self.watch.wall - 7))["status"],
                         "ignored")
        latest = self.store.recap("a")
        self.assertEqual((0.1, None), (latest["timer_minutes"], latest["average_hr"]))

    def test_retries_do_not_extend_retention_and_capacity_does_not_evict_an_active_owner(self):
        store = LiveRunStore(clock=lambda: self.watch.wall, monotonic=lambda: self.watch.elapsed, max_owners=1)
        old = self.sample()
        store.ingest("a", old)
        with self.assertRaises(ValueError):
            store.ingest("b", self.sample())
        self.advance(RETENTION_SECONDS-1)
        self.assertEqual(store.ingest("a", old)["status"], "duplicate")
        self.advance(1)
        self.assertIsNone(store.recap("a"))
        self.assertEqual(store.ingest("a", old)["status"], "ignored")
        self.assertIsNone(store.recap("a"))
        self.assertEqual(store.ingest("b", self.sample())["status"], "accepted")

    def test_malformed_frames_leave_the_previous_frame_intact(self):
        good = self.sample(timer_ms=5000)
        self.store.ingest("a", good)
        bad_inputs = [None, [], {k: v for k, v in good.items() if k != "speed_mps"},
                      {**good, "owner_id": "b"}]
        for key, value in (("state", "walking"), ("session_id", ""), ("session_id", "x"*129),
                           ("seq", True), ("seq", -1), ("observed_at", 1.5), ("observed_at", False),
                           ("heart_rate_bpm", True), ("heart_rate_bpm", "121"), ("cadence_rpm", -3),
                           ("speed_mps", False), ("speed_mps", -1), ("speed_mps", float("nan")),
                           ("speed_mps", float("inf"))):
            bad_inputs.append({**good, "seq": 1, key: value})
        for bad in bad_inputs:
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                self.store.ingest("a", bad)
        for owner in (None, "", " "):
            with self.subTest(owner=owner), self.assertRaises(ValueError):
                self.store.ingest(owner, good)
        self.assertEqual(self.store.recap("a")["average_hr"], good["heart_rate_bpm"])

    def test_concurrent_out_of_order_writes_converge_on_highest_sequence(self):
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
            list(executor.map(lambda seq: self.store.ingest("a", self.sample(seq=seq, timer_ms=seq * 6000)),
                              list(range(30, -1, -1)) + list(range(31))))
        self.assertEqual(3.0, self.store.recap("a")["timer_minutes"])


class RunRecapTest(unittest.TestCase):
    def setUp(self):
        self.watch = WatchRun()

    def test_a_finished_run_is_held_a_day_after_its_last_sample(self):
        self.watch.run_for(30)
        self.watch.control("ended")
        self.watch.advance(RETENTION_SECONDS - 1)
        recap = self.watch.recap()
        self.assertEqual(("ended", 30.0, 130), (recap["state"], recap["timer_minutes"], recap["average_hr"]))
        self.watch.advance(1)
        self.assertIsNone(self.watch.recap())

    def test_a_new_run_replaces_the_finished_one(self):
        self.watch.run_for(30)
        self.watch.control("ended")
        self.watch.advance(3600)
        self.watch.session, self.watch.seq, self.watch.timer_ms, self.watch.distance = "run-b", 0, 0, 0.0
        self.watch.run_for(2, heart_rate=150)
        recap = self.watch.recap()
        self.assertEqual(("running", 2.0, 150), (recap["state"], recap["timer_minutes"], recap["average_hr"]))
        self.assertEqual(1, len(recap["every_10_minutes"]))

    def test_the_summary_covers_every_sample_of_a_long_run(self):
        self.watch.run_for(20, heart_rate=120)
        self.watch.run_for(170, heart_rate=140)
        self.watch.run_for(20, heart_rate=160)
        recap = self.watch.recap()
        self.assertEqual(("2027-01-15T08:00:00Z", 210.0, 210.0),
                         (recap["started_at"], recap["timer_minutes"], recap["moving_minutes"]))
        self.assertEqual(35.28, recap["distance_km"])
        self.assertEqual((357, 140, 160, 168), (recap["average_pace_sec_per_km"], recap["average_hr"],
                                               recap["max_hr"], recap["average_steps_per_minute"]))
        self.assertEqual(list(range(0, 210, 10)), [row["from_minute"] for row in recap["every_10_minutes"]])
        self.assertEqual(120, recap["every_10_minutes"][0]["average_hr"])
        self.assertEqual(160, recap["every_10_minutes"][-1]["average_hr"])

    def test_a_session_past_the_sample_limit_is_thinned_not_cut(self):
        with mock.patch.object(live_run, "MAX_RUN_SAMPLES", 50):
            self.watch.run_for(5, heart_rate=120)
            self.watch.run_for(5, heart_rate=160)
        recap = self.watch.recap()
        self.assertEqual(("2027-01-15T08:00:00Z", 10.0), (recap["started_at"], recap["timer_minutes"]))
        self.assertEqual([0], [row["from_minute"] for row in recap["every_10_minutes"]])
        self.assertLess(120, recap["average_hr"])
        self.assertLess(recap["average_hr"], 160)

    def test_thirds_take_the_shape_of_run_drift_and_need_fifteen_minutes(self):
        rates = itertools.count(120)
        self.watch.run_for(14, heart_rate=lambda: next(rates))
        self.assertEqual((None, None), (self.watch.recap()["first_third"], self.watch.recap()["last_third"]))
        self.watch.run_for(16, speed=2.5, cadence=80, heart_rate=lambda: next(rates))
        recap = self.watch.recap()
        first, last = recap["first_third"], recap["last_third"]
        self.assertEqual({"average_hr", "average_pace_sec_per_km", "average_steps_per_minute"}, set(first))
        self.assertGreater(last["average_hr"], first["average_hr"])
        self.assertEqual((357, 400), (first["average_pace_sec_per_km"], last["average_pace_sec_per_km"]))
        self.assertEqual((168, 160), (first["average_steps_per_minute"], last["average_steps_per_minute"]))

    def test_standing_with_the_timer_running_is_a_stop_and_not_moving_time(self):
        self.watch.run_for(10)
        self.watch.run_for(0.5, speed=0.0, cadence=0)
        self.watch.run_for(10)
        recap = self.watch.recap()
        self.assertEqual([{"from_minute": 10.0, "seconds": 30, "kind": "stopped"}], recap["stopped_or_walking"])
        self.assertEqual((20.5, 20.0), (recap["timer_minutes"], recap["moving_minutes"]))
        self.assertEqual(357, recap["average_pace_sec_per_km"])

    def test_a_paused_timer_is_a_stop(self):
        self.watch.run_for(10)
        self.watch.control("paused")
        self.watch.advance(44)
        self.watch.run_for(10)
        recap = self.watch.recap()
        self.assertEqual([{"from_minute": 10.0, "seconds": 45, "kind": "stopped"}], recap["stopped_or_walking"])
        self.assertEqual((20.0, 20.0), (recap["timer_minutes"], recap["moving_minutes"]))

    def test_walking_is_a_drop_in_cadence_not_a_slow_pace(self):
        self.watch.run_for(20, speed=1.9, cadence=80)
        self.watch.run_for(3, speed=1.6, climb_per_minute=12, cadence=78)
        self.assertEqual([], self.watch.recap()["stopped_or_walking"])
        self.watch.run_for(2, speed=1.4, cadence=56)
        self.watch.run_for(5, speed=1.9, cadence=80)
        self.assertEqual([{"from_minute": 23.0, "seconds": 120, "kind": "walking"}],
                         self.watch.recap()["stopped_or_walking"])

    def test_a_short_slow_moment_is_not_a_stretch(self):
        self.watch.run_for(10)
        self.watch.run_for(1 / 12, speed=0.0, cadence=0)
        self.watch.run_for(10)
        self.assertEqual([], self.watch.recap()["stopped_or_walking"])

    def test_samples_missing_for_a_while_are_neither_a_stop_nor_a_walk(self):
        self.watch.run_for(10)
        self.watch.advance(115)
        self.watch.timer_ms += 115_000
        self.watch.run_for(1 / 12, speed=0.0, cadence=0)
        self.watch.run_for(10)
        recap = self.watch.recap()
        self.assertEqual([], recap["stopped_or_walking"])
        self.assertEqual(recap["timer_minutes"], recap["moving_minutes"])

    def test_rows_tell_a_hill_from_a_faster_pace(self):
        self.watch.run_for(10)
        self.watch.run_for(10, speed=2.2, climb_per_minute=6, heart_rate=150, cadence=80)
        flat, hill = self.watch.recap()["every_10_minutes"]
        self.assertEqual((0.0, 357, 130), (flat["climb_m"], flat["average_pace_sec_per_km"], flat["average_hr"]))
        self.assertEqual((60.0, 455, 150, 160), (hill["climb_m"], hill["average_pace_sec_per_km"],
                                                hill["average_hr"], hill["average_steps_per_minute"]))

    def test_a_reading_the_watch_did_not_send_stays_null(self):
        self.watch.run_for(20, heart_rate=None, altitude_m=None, distance_m=None, cadence=None,
                           ascent_m=None, descent_m=None)
        recap = self.watch.recap()
        for key in ("distance_km", "average_pace_sec_per_km", "average_hr", "max_hr", "average_steps_per_minute",
                    "ascent_m", "descent_m", "first_third", "last_third"):
            self.assertIsNone(recap[key], key)
        for key in ("average_hr", "average_pace_sec_per_km", "average_steps_per_minute", "climb_m"):
            self.assertIsNone(recap["every_10_minutes"][0][key], key)

    def test_workout_steps_appear_only_once_the_watch_counted_one(self):
        self.watch.run_for(5)
        self.assertNotIn("workout_steps_done", self.watch.recap())
        self.watch.run_for(5, steps_done=3)
        self.assertEqual(3, self.watch.recap()["workout_steps_done"])

    def test_a_watch_without_the_new_measurements_still_reports_its_state(self):
        receipt = self.watch.store.ingest("a", {"session_id": "old", "seq": 0, "observed_at": self.watch.wall,
                                                "state": "running", "heart_rate_bpm": 120, "cadence_rpm": 80,
                                                "speed_mps": 2.0})
        self.assertEqual("accepted", receipt["status"])
        recap = self.watch.recap()
        self.assertEqual("running", recap["state"])
        self.assertNotIn("timer_minutes", recap)

    def test_unknown_or_impossible_measurements_are_refused(self):
        for bad in ({"gps": [1, 2]}, {"distance_m": -1}, {"timer_ms": float("nan")}, {"power_w": "high"}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                self.watch.store.ingest("a", self.watch.sample(**bad))
        self.assertEqual("accepted", self.watch.store.ingest("a", self.watch.sample(altitude_m=-3.5))["status"])


if __name__ == "__main__":
    unittest.main()
