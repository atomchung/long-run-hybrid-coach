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
