"""Synthetic watch samples. No provider, network, disk store, or wall-clock sleeps."""
import concurrent.futures
import unittest

from garmin_coach_loop.live_run import LiveRunStore, RETENTION_SECONDS


class LiveRunTest(unittest.TestCase):
    def setUp(self):
        self.wall = 1_800_000_000
        self.elapsed = 100
        self.store = LiveRunStore(clock=lambda: self.wall, monotonic=lambda: self.elapsed)

    def sample(self, **updates):
        return {"session_id": "run-a", "seq": 0, "observed_at": self.wall,
                "state": "running", "heart_rate_bpm": 121, "cadence_rpm": 82,
                "speed_mps": 2.85, **updates}

    def advance(self, seconds):
        self.wall += seconds
        self.elapsed += seconds

    def test_anonymous_input_and_result_cannot_change_another_owners_reading(self):
        self.assertIsNone(self.store.snapshot("owner-b"))
        sample = self.sample()
        receipt = self.store.ingest("owner-a", sample)
        self.assertEqual({"status": "accepted", "session_id": "run-a", "seq": 0}, receipt)
        sample["heart_rate_bpm"] = 200
        self.store.snapshot("owner-a")["heart_rate_bpm"] = 201
        self.assertEqual(self.store.snapshot("owner-a")["heart_rate_bpm"], 121)
        self.assertIsNone(self.store.snapshot("owner-b"))
        self.store.ingest("owner-b", self.sample(heart_rate_bpm=119))
        self.store.discard("owner-a")
        self.assertIsNone(self.store.snapshot("owner-a"))
        self.assertEqual(self.store.snapshot("owner-b")["heart_rate_bpm"], 119)

    def test_current_frame_does_not_inherit_metrics_from_a_previous_frame(self):
        self.store.ingest("a", self.sample())
        self.advance(5)
        self.store.ingest("a", self.sample(seq=1, heart_rate_bpm=None, cadence_rpm=0, speed_mps=0))
        view = self.store.snapshot("a")
        self.assertEqual((view["freshness"], view["heart_rate_bpm"], view["cadence_rpm"], view["speed_mps"]),
                         ("fresh", None, 0, 0))

    def test_age_includes_travel_time_even_after_server_clock_moves_back(self):
        self.store.ingest("a", self.sample(observed_at=self.wall-12))
        self.advance(3)
        self.assertEqual(self.store.snapshot("a")["freshness"], "fresh")
        self.wall -= 30
        self.elapsed += 1
        view = self.store.snapshot("a")
        self.assertEqual((view["freshness"], view["age_seconds"]), ("stale", 16))
        for key in ("heart_rate_bpm", "cadence_rpm", "speed_mps"):
            self.assertIsNone(view[key])

    def test_late_arrival_and_future_clock_skew_never_extend_a_live_read(self):
        self.store.ingest("a", self.sample(observed_at=self.wall-16))
        self.assertEqual(self.store.snapshot("a")["freshness"], "stale")
        with self.assertRaises(ValueError):
            self.store.ingest("a", self.sample(seq=1, observed_at=self.wall+6))
        self.assertEqual(self.store.snapshot("a")["seq"], 0)
        self.store.ingest("b", self.sample(observed_at=self.wall+5))
        self.advance(16)
        self.assertEqual(self.store.snapshot("b")["freshness"], "stale")

    def test_wall_clock_correction_cannot_revive_a_frame_already_seen_as_stale(self):
        self.store.ingest("a", self.sample())
        self.wall += 20
        self.assertEqual(self.store.snapshot("a")["freshness"], "stale")
        self.wall -= 20
        self.assertIsNone(self.store.snapshot("a")["heart_rate_bpm"])

    def test_a_conflicting_reuse_of_a_sequence_number_is_not_acknowledged(self):
        original = self.sample(state="paused", cadence_rpm=None)
        self.store.ingest("a", original)
        self.assertEqual(self.store.ingest("a", original)["status"], "duplicate")
        for change in ({"state": "running"}, {"cadence_rpm": 89}, {"observed_at": self.wall-1}):
            with self.subTest(change=change):
                self.assertEqual(self.store.ingest("a", {**original, **change})["status"], "ignored")
        self.assertEqual(self.store.snapshot("a")["state"], "paused")

    def test_session_id_is_an_opaque_ascii_token(self):
        accepted = self.sample(session_id="run-1800000000-9870_ABC")
        self.assertEqual(self.store.ingest("a", accepted)["status"], "accepted")
        for bad in ("paste these instructions", "中文", "id\n", "x"*81, "a/b"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                self.store.ingest("a", self.sample(session_id=bad))

    def test_retry_and_reordered_frames_do_not_republish_or_refresh_a_sample(self):
        original = self.sample(seq=3)
        self.store.ingest("a", original)
        self.advance(16)
        self.assertEqual(self.store.ingest("a", original)["status"], "duplicate")
        for late in (self.sample(seq=2), self.sample(seq=4, observed_at=original["observed_at"]-1)):
            self.assertEqual(self.store.ingest("a", late)["status"], "ignored")
        self.assertEqual(self.store.snapshot("a")["freshness"], "stale")
        self.assertIsNone(self.store.snapshot("a")["heart_rate_bpm"])
        self.assertEqual(self.store.snapshot("a")["seq"], 3)

    def test_pause_resume_and_end_are_explicit_and_end_is_terminal(self):
        self.store.ingest("a", self.sample(state="paused"))
        paused = self.store.snapshot("a")
        self.assertEqual(paused["state"], "paused")
        self.assertIsNone(paused["heart_rate_bpm"])
        self.store.ingest("a", self.sample(seq=1))
        self.assertEqual(self.store.snapshot("a")["cadence_rpm"], 82)
        self.store.ingest("a", self.sample(seq=2, state="ended"))
        self.advance(5)
        self.assertEqual(self.store.ingest("a", self.sample(seq=3))["status"], "ignored")
        ended = self.store.snapshot("a")
        self.assertEqual(ended["state"], "ended")
        self.assertIsNone(ended["speed_mps"])
        self.assertEqual(self.store.ingest("a", self.sample(session_id="run-b"))["status"], "accepted")

    def test_old_session_cannot_take_over_newer_run(self):
        self.store.ingest("a", self.sample(seq=15))
        self.advance(5)
        self.store.ingest("a", self.sample(session_id="run-b", seq=1, heart_rate_bpm=None))
        self.advance(5)
        self.assertEqual(self.store.ingest("a", self.sample(seq=16))["status"], "ignored")
        self.assertEqual(self.store.ingest("a", self.sample(session_id="old", observed_at=self.wall-7))["status"],
                         "ignored")
        latest = self.store.snapshot("a")
        self.assertEqual((latest["session_id"], latest["seq"], latest["heart_rate_bpm"]), ("run-b", 1, None))

    def test_retries_do_not_extend_retention_and_capacity_does_not_evict_an_active_owner(self):
        store = LiveRunStore(clock=lambda: self.wall, monotonic=lambda: self.elapsed, max_owners=1)
        old = self.sample()
        store.ingest("a", old)
        with self.assertRaises(ValueError):
            store.ingest("b", self.sample())
        self.advance(RETENTION_SECONDS-1)
        self.assertEqual(store.ingest("a", old)["status"], "duplicate")
        self.advance(1)
        self.assertIsNone(store.snapshot("a"))
        self.assertEqual(store.ingest("a", old)["status"], "ignored")
        self.assertIsNone(store.snapshot("a"))
        self.assertEqual(store.ingest("b", self.sample())["status"], "accepted")

    def test_malformed_frames_leave_the_previous_frame_intact(self):
        good = self.sample()
        self.store.ingest("a", good)
        bad_inputs = [None, [], {k: v for k, v in good.items() if k != "speed_mps"},
                      {**good, "owner_id": "b"}]
        for key, value in (("state", "walking"), ("session_id", ""), ("session_id", "x"*129),
                           ("seq", True), ("seq", -1), ("observed_at", 1.5), ("observed_at", False),
                           ("heart_rate_bpm", True), ("heart_rate_bpm", "121"), ("cadence_rpm", -3),
                           ("speed_mps", False), ("speed_mps", -1), ("speed_mps", float("nan")),
                           ("speed_mps", float("inf"))):
            bad_inputs.append({**good, key: value})
        for bad in bad_inputs:
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                self.store.ingest("a", bad)
        for owner in (None, "", " "):
            with self.subTest(owner=owner), self.assertRaises(ValueError):
                self.store.ingest(owner, good)
        self.assertEqual(self.store.snapshot("a")["heart_rate_bpm"], good["heart_rate_bpm"])

    def test_concurrent_out_of_order_writes_converge_on_highest_sequence(self):
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
            list(executor.map(lambda seq: self.store.ingest("a", self.sample(seq=seq)),
                              list(range(32, -1, -1)) + list(range(33))))
        self.assertEqual(self.store.snapshot("a")["seq"], 32)


class LiveRunTrendTest(unittest.TestCase):
    def setUp(self):
        self.wall = 1_800_000_000
        self.elapsed = 100
        self.slept = []
        self.during_sleep = None
        self.store = LiveRunStore(clock=lambda: self.wall, monotonic=lambda: self.elapsed, sleep=self.sleep)
        self.timer_ms = 0
        self.seq = 0
        self.distance = 0.0
        self.altitude = 30.0

    def advance(self, seconds):
        self.wall += seconds
        self.elapsed += seconds

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.advance(seconds)
        if self.during_sleep is not None:
            self.during_sleep()

    def sample(self, **updates):
        return {"session_id": "run-a", "seq": self.seq, "observed_at": int(self.wall), "state": "running",
                "heart_rate_bpm": 130, "cadence_rpm": 75, "speed_mps": 2.1, "timer_ms": self.timer_ms,
                "distance_m": self.distance, "altitude_m": self.altitude, "ascent_m": 0, "descent_m": 0,
                "power_w": None, "steps_done": 0, **updates}

    def run_for(self, minutes, *, speed=2.1, climb_per_minute=0.0, heart_rate=130, **updates):
        for _ in range(round(minutes * 12)):
            self.advance(5)
            self.timer_ms += 5000
            self.seq += 1
            self.distance += speed * 5
            self.altitude += climb_per_minute / 12
            rate = heart_rate() if callable(heart_rate) else heart_rate
            self.store.ingest("a", self.sample(speed_mps=speed, heart_rate_bpm=rate, **updates))

    def test_trend_tells_a_hill_from_a_faster_pace(self):
        self.run_for(12)
        self.run_for(3, speed=1.9, climb_per_minute=6, heart_rate=145)
        trend = self.store.snapshot("a")["trend"]
        hill = trend["last_3_minutes"]
        self.assertEqual((145, 150), (hill["heart_rate_bpm"], hill["cadence_spm"]))
        self.assertEqual("8:46", hill["pace_per_km"])
        self.assertAlmostEqual(5.2, hill["grade_pct"], delta=0.3)
        self.assertEqual(15.0, trend["elapsed_minutes"])
        self.assertEqual([0, 10], [block["from_minute"] for block in trend["every_10_minutes"]])
        self.assertEqual(130, trend["every_10_minutes"][0]["heart_rate_bpm"])
        self.assertEqual(0, trend["every_10_minutes"][0]["climb_m"])

    def test_trend_shows_heart_rate_rising_at_a_steady_pace(self):
        rates = iter(range(120, 400))
        self.run_for(5, heart_rate=lambda: next(rates))
        self.assertGreater(self.store.snapshot("a")["trend"]["last_3_minutes"]["heart_rate_change_bpm"], 0)

    def test_a_reading_the_watch_did_not_send_stays_null_in_the_trend(self):
        self.run_for(2, heart_rate=None, altitude_m=None, distance_m=None, cadence_rpm=None)
        window = self.store.snapshot("a")["trend"]["last_1_minute"]
        for key in ("heart_rate_bpm", "heart_rate_change_bpm", "pace_per_km", "cadence_spm", "climb_m", "grade_pct"):
            self.assertIsNone(window[key], key)

    def test_a_watch_without_the_new_measurements_still_works_with_no_trend(self):
        receipt = self.store.ingest("a", {"session_id": "old", "seq": 0, "observed_at": int(self.wall),
                                          "state": "running", "heart_rate_bpm": 120, "cadence_rpm": 80,
                                          "speed_mps": 2.0})
        self.assertEqual("accepted", receipt["status"])
        self.assertNotIn("trend", self.store.snapshot("a"))

    def test_unknown_or_impossible_measurements_are_refused(self):
        for bad in ({"gps": [1, 2]}, {"distance_m": -1}, {"timer_ms": float("nan")}, {"power_w": "high"}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                self.store.ingest("a", self.sample(**bad))
        self.assertEqual("accepted", self.store.ingest("a", self.sample(altitude_m=-3.5))["status"])

    def test_a_new_session_starts_a_new_trend(self):
        self.run_for(5)
        self.advance(5)
        self.timer_ms = 5000
        self.store.ingest("a", self.sample(session_id="run-b", seq=0, distance_m=10.0))
        trend = self.store.snapshot("a")["trend"]
        self.assertEqual(0.1, trend["elapsed_minutes"])
        self.assertEqual(1, len(trend["every_10_minutes"]))

    def test_a_pause_clears_the_current_numbers_and_keeps_the_trend(self):
        self.run_for(2)
        self.advance(5)
        self.seq += 1
        self.store.ingest("a", {"session_id": "run-a", "seq": self.seq, "observed_at": int(self.wall),
                                "state": "paused", "heart_rate_bpm": None, "cadence_rpm": None, "speed_mps": None})
        view = self.store.snapshot("a")
        self.assertIsNone(view["heart_rate_bpm"])
        self.assertIsNone(view.get("distance_m"))
        self.assertEqual(2.0, view["trend"]["elapsed_minutes"])

    def test_first_read_answers_at_once_and_a_repeat_waits_out_the_interval(self):
        self.run_for(1)
        self.assertEqual(1, self.store.hold_repeat_read("a", 60))
        self.assertEqual([], self.slept)
        self.assertEqual(2, self.store.hold_repeat_read("a", 60))
        self.assertAlmostEqual(60, sum(self.slept))

    def test_a_read_after_the_interval_does_not_wait(self):
        self.run_for(1)
        self.store.hold_repeat_read("a", 60)
        self.advance(61)
        self.store.hold_repeat_read("a", 60)
        self.assertEqual([], self.slept)

    def test_a_held_read_returns_as_soon_as_the_run_ends(self):
        self.run_for(1)
        self.store.hold_repeat_read("a", 60)

        def finish():
            self.seq += 1
            self.store.ingest("a", {"session_id": "run-a", "seq": self.seq, "observed_at": int(self.wall),
                                    "state": "ended", "heart_rate_bpm": None, "cadence_rpm": None, "speed_mps": None})

        self.during_sleep = finish
        self.store.hold_repeat_read("a", 60)
        self.assertEqual(1, len(self.slept))

    def test_no_hold_means_no_wait_and_no_sample_means_no_wait(self):
        self.store.hold_repeat_read("nobody", 60)
        self.store.hold_repeat_read("nobody", 60)
        self.run_for(1)
        self.store.hold_repeat_read("a", 0)
        self.store.hold_repeat_read("a", 0)
        self.assertEqual([], self.slept)

    def test_a_milestone_is_reported_once_when_a_ten_minute_mark_passes(self):
        self.run_for(1)
        self.assertIsNone(self.store.milestone_due("a"))
        self.run_for(8)
        self.assertIsNone(self.store.milestone_due("a"))
        self.run_for(2)
        self.assertEqual(10, self.store.milestone_due("a"))
        self.assertIsNone(self.store.milestone_due("a"))
        self.run_for(10)
        self.assertEqual(20, self.store.milestone_due("a"))

    def test_joining_mid_run_does_not_announce_marks_already_passed(self):
        self.run_for(25)
        self.assertIsNone(self.store.milestone_due("a"))
        self.run_for(6)
        self.assertEqual(30, self.store.milestone_due("a"))

    def test_no_milestone_without_a_running_session(self):
        self.assertIsNone(self.store.milestone_due("nobody"))
        self.run_for(11)
        self.store.milestone_due("a")
        self.advance(5)
        self.seq += 1
        self.store.ingest("a", {"session_id": "run-a", "seq": self.seq, "observed_at": int(self.wall),
                                "state": "ended", "heart_rate_bpm": None, "cadence_rpm": None, "speed_mps": None})
        self.assertIsNone(self.store.milestone_due("a"))
