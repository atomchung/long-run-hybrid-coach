"""The in-memory count the watch routes are held to."""

import unittest

from garmin_coach_loop.rate_limit import RateLimiter


class RateLimiterTests(unittest.TestCase):
    def setUp(self):
        self.now = [100.0]
        self.limiter = RateLimiter(3, 60, max_keys=2, clock=lambda: self.now[0])

    def test_a_burst_up_to_the_limit_then_a_wait(self):
        self.assertEqual([None, None, None], [self.limiter.allow("a") for _ in range(3)])
        self.assertEqual(20, self.limiter.allow("a"))
        self.now[0] += 20
        self.assertIsNone(self.limiter.allow("a"))
        self.assertEqual(20, self.limiter.allow("a"))

    def test_a_refused_call_does_not_spend_the_next_slot(self):
        for _ in range(3):
            self.limiter.allow("a")
        for _ in range(5):
            self.assertIsNotNone(self.limiter.allow("a"))
        self.now[0] += 20
        self.assertIsNone(self.limiter.allow("a"))

    def test_keys_are_counted_apart_and_the_oldest_is_forgotten_past_the_bound(self):
        for _ in range(3):
            self.limiter.allow("a")
        self.assertIsNone(self.limiter.allow("b"))
        self.assertIsNotNone(self.limiter.allow("a"))
        self.assertIsNone(self.limiter.allow("c"))  # "b" is now the oldest kept, "a" the newest
        self.assertIsNone(self.limiter.allow("d"))
        # "a" was forgotten, so it starts from a full bucket again rather than growing memory.
        self.assertIsNone(self.limiter.allow("a"))

    def test_a_clock_that_steps_back_grants_nothing_extra(self):
        for _ in range(3):
            self.limiter.allow("a")
        self.now[0] -= 1000
        self.assertIsNotNone(self.limiter.allow("a"))

    def test_bad_settings_are_refused(self):
        for args in ((0, 60), (3, 0), (True, 60), (3, -1)):
            with self.subTest(args=args), self.assertRaises(ValueError):
                RateLimiter(*args)


if __name__ == "__main__":
    unittest.main()
