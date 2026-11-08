"""The watch data field over HTTP: pairing, uploads, and the reads that carry the run."""

import json
import tempfile
import threading
import time
import urllib.error
import urllib.request
import unittest
from dataclasses import replace
from pathlib import Path

from garmin_coach_loop import connected_account, product_scope
from garmin_coach_loop.gateway import (
    LIVE_RUN_PATH, MCP_PATH, WATCH_FIELD_SENDS, WATCH_PAIRING_PATH, WATCH_PAIRING_POLL_PATH,
    WATCH_ROUTES, WATCH_RUN_KEPT, WATCH_STOP, CoachGateway, CoachGatewayHandler, CoachGatewayServer,
    GatewayConfig, GatewayError, run_preflight,
)
from garmin_coach_loop.identity import (
    lookup_or_create_owner, record_token_fingerprint, token_fingerprint, watch_device,
)
from garmin_coach_loop.live_run import RETENTION_SECONDS, LiveRunStore
from garmin_coach_loop.rate_limit import RateLimiter
from tests.test_gateway import (
    CLIENT_ID_VALUE, CLIENT_SECRET_VALUE, HMAC_KEY, NOW, TOKEN_A, TOKEN_B, FakeIntervals, GatewayTestCase,
    athlete_profile, publishable_plan,
)


SECRET = "a1" * 32
OTHER_SECRET = "b2" * 32


def running(seq, timer_ms, observed_at, **updates):
    return {"session_id": "fixture-1", "seq": seq, "observed_at": observed_at, "state": "running",
            "heart_rate_bpm": 140, "cadence_rpm": 84, "speed_mps": 2.8, "timer_ms": timer_ms,
            "distance_m": timer_ms * 0.0028, "altitude_m": 30.0, "ascent_m": 4, "descent_m": 3,
            "power_w": None, "steps_done": 0, **updates}


class WatchGatewayTestCase(GatewayTestCase):
    """A full-product gateway with two accounts, A holding a plan."""

    def setUp(self):
        super().setUp()
        self.owner = self.seed_owner(TOKEN_A, plan=publishable_plan())
        self.other = self.seed_owner(TOKEN_B, athlete_id="i2")

    def tool(self, token, name, arguments=None):
        status, reply = self.call("POST", MCP_PATH, body=self.tool_rpc(name, arguments),
                                  token=self.mcp_bearer(token))
        self.assertEqual(200, status, reply)
        return reply["result"]

    def read(self, token, tool="getCoachState"):
        result = self.tool(token, tool)
        self.assertFalse(result.get("isError"), result)
        return result["structuredContent"]

    def start(self, secret=SECRET, headers=None):
        status, started = self.call("POST", WATCH_PAIRING_PATH, body={"secret": secret}, headers=headers)
        self.assertEqual(200, status, started)
        return started

    def poll(self, code, secret=SECRET, headers=None):
        status, polled = self.call("POST", WATCH_PAIRING_POLL_PATH, body={"code": code, "secret": secret},
                                   headers=headers)
        self.assertEqual(200, status, polled)
        return polled

    def pair(self, token=TOKEN_A, secret=SECRET):
        code = self.start(secret)["code"]
        self.assertEqual({"status": "pending"}, self.poll(code, secret))
        said = self.tool(token, "connectWatch", {"code": code})["structuredContent"]
        self.assertEqual("connected", said["status"], said)
        issued = self.poll(code, secret)
        self.assertEqual("issued", issued["status"])
        self.last_code = code
        return issued["credential"]

    def raw_post(self, path, body, *, token=None, headers=None):
        request = urllib.request.Request(self.base_url + path, data=json.dumps(body).encode(), method="POST",
                                         headers={"Content-Type": "application/json", **(headers or {})})
        if token is not None:
            request.add_header("Authorization", "Bearer " + token)
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, dict(response.headers), json.loads(response.read() or b"{}")
        except urllib.error.HTTPError as exc:
            with exc:
                return exc.code, dict(exc.headers), json.loads(exc.read() or b"{}")


class WatchPairingTests(WatchGatewayTestCase):
    def test_a_code_said_in_the_conversation_pairs_the_watch_to_that_account(self):
        started = self.start()
        self.assertEqual({"code", "expires_in", "interval"}, set(started))
        self.assertEqual((8, 600, 5), (len(started["code"]), started["expires_in"], started["interval"]))
        code = started["code"]
        self.assertEqual({"status": "pending"}, self.poll(code))
        said = self.tool(TOKEN_A, "connectWatch", {"code": code.lower()[:4] + " " + code[4:]})
        self.assertFalse(said.get("isError"), said)
        result = said["structuredContent"]
        label = connected_account.masked_email(athlete_profile("i1")["email"])
        self.assertEqual(("connected", label), (result["status"], result["account_label"]))
        self.assertEqual((WATCH_FIELD_SENDS, WATCH_RUN_KEPT, WATCH_STOP),
                         (result["sends"], result["kept"], result["stop"]))
        self.assertIn(label, result["watch"])
        self.assertNotIn("api_version", result)
        issued = self.poll(code)
        self.assertEqual(("issued", label), (issued["status"], issued["account_label"]))
        self.assertGreaterEqual(len(issued["credential"]), 32)
        # One credential per pairing, and the same code said again is the same pairing.
        self.assertEqual({"status": "invalid"}, self.poll(code))
        again = self.tool(TOKEN_A, "connectWatch", {"code": code})["structuredContent"]
        self.assertEqual("connected", again["status"])
        self.assertEqual({"paired_at", "last_seen_at", "account_label"},
                         set(watch_device(self.identity_db, self.owner)))
        # Nothing that opens anything reaches a log line: not the code, not either secret.
        for secret in (code, SECRET, issued["credential"]):
            self.assertFalse(any(secret in line for line in self.log_handler.records), secret)
        # Asked, said, issued, refused on the second poll, said again.
        events = [line for line in self.log_handler.records if '"event": "watch_pairing"' in line]
        self.assertEqual(5, len(events), events)

    def test_a_code_that_is_wrong_expired_or_taken_is_not_accepted_alike(self):
        code = self.start()["code"]
        self.assertEqual("connected", self.tool(TOKEN_A, "connectWatch", {"code": code})["structuredContent"]["status"])
        other_code = self.start(OTHER_SECRET)["code"]
        # Taken by another account while it is still live, then each other kind after.
        refusals = [self.tool(TOKEN_B, "connectWatch", {"code": code})["structuredContent"]]
        self.now = NOW.replace(minute=11)
        refusals += [
            self.tool(TOKEN_B, "connectWatch", {"code": other_code})["structuredContent"],  # expired
            self.tool(TOKEN_B, "connectWatch", {"code": "ZZZZ2222"})["structuredContent"],  # unknown
            self.tool(TOKEN_B, "connectWatch", {"code": "not a code"})["structuredContent"],
        ]
        for refusal in refusals:
            self.assertEqual({"status", "detail"}, set(refusal))
            self.assertEqual(refusals[0], refusal)
        self.assertEqual("code_not_accepted", refusals[0]["status"])
        self.assertIsNone(watch_device(self.identity_db, self.other))

    def test_a_wrong_secret_never_receives_the_credential(self):
        code = self.start()["code"]
        self.tool(TOKEN_A, "connectWatch", {"code": code})
        self.assertEqual({"status": "invalid"}, self.poll(code, OTHER_SECRET))
        self.assertEqual("issued", self.poll(code)["status"])

    def test_a_secret_that_is_not_one_is_refused_before_anything_is_stored(self):
        for body in ({}, {"secret": "short"}, {"secret": SECRET, "owner_id": self.owner},
                     {"secret": "é" * 40}, {"secret": 12345678901234567890123456789012345}):
            with self.subTest(body=body):
                self.assertEqual(400, self.call("POST", WATCH_PAIRING_PATH, body=body)[0])

    def test_an_unreadable_email_costs_the_label_not_the_pairing(self):
        self.fake.profile_status = 403
        code = self.start()["code"]
        result = self.tool(TOKEN_A, "connectWatch", {"code": code})["structuredContent"]
        self.assertEqual(("connected", None), (result["status"], result["account_label"]))
        issued = self.poll(code)
        self.assertEqual(("issued", None), (issued["status"], issued["account_label"]))

    def test_pairing_another_watch_retires_the_first_credential(self):
        first = self.pair()
        self.gateway._watch_upload_limit = RateLimiter(10_000, 60)
        sample = running(1, 5000, int(time.time()))
        self.assertEqual(200, self.call("POST", LIVE_RUN_PATH, token=first, body=sample)[0])
        first_code = self.last_code
        second = self.pair(secret=OTHER_SECRET)
        status, refused = self.call("POST", LIVE_RUN_PATH, token=first, body=running(2, 10000, int(time.time())))
        self.assertEqual((401, "invalid_device_credential"), (status, refused["error"]))
        # The first watch's code, said again, no longer reports a connection it lost.
        again = self.tool(TOKEN_A, "connectWatch", {"code": first_code})["structuredContent"]
        self.assertEqual("code_not_accepted", again["status"])
        self.assertEqual(200, self.call("POST", LIVE_RUN_PATH, token=second,
                                        body=running(2, 10000, int(time.time())))[0])


class WatchRunReadTests(WatchGatewayTestCase):
    def setUp(self):
        super().setUp()
        self.watch = self.pair()
        # A whole run arrives here in one burst; a real watch spends 20 minutes on it.
        self.gateway._watch_upload_limit = RateLimiter(10_000, 60)
        self.gateway._watch_address_limits["live_run_samples"] = RateLimiter(10_000, 60)
        self.sample = running(1, 5000, int(time.time()))

    def post(self, sample):
        status, ack = self.call("POST", LIVE_RUN_PATH, token=self.watch, body=sample)
        self.assertEqual(200, status, ack)
        return ack

    def finish_a_run(self, minutes=20, start=None):
        start = int(time.time()) - minutes * 60 - 30 if start is None else start
        for seq in range(1, minutes * 12 + 1):
            self.assertEqual("accepted", self.post(running(seq, seq * 5000, start + seq * 5))["status"])
        self.post({"session_id": "fixture-1", "seq": minutes * 12 + 1, "observed_at": start + minutes * 60 + 3,
                   "state": "ended", "heart_rate_bpm": None, "cadence_rpm": None, "speed_mps": None})

    def test_a_finished_run_reaches_both_reads_for_its_owner_only(self):
        self.assertIsNone(self.read(TOKEN_A)["watch_run"])
        provider_calls = len(self.fake.calls)
        self.finish_a_run()
        self.assertEqual(provider_calls, len(self.fake.calls))
        for tool in ("getCoachState", "startCoachSession"):
            with self.subTest(tool=tool):
                recap = self.read(TOKEN_A, tool)["watch_run"]
                self.assertEqual(("garmin_watch", "ended", 20.0, 3.36, 140),
                                 (recap["source"], recap["state"], recap["timer_minutes"], recap["distance_km"],
                                  recap["average_hr"]))
        # An account with no paired watch reads exactly what it read before watches existed.
        self.assertNotIn("watch_run", self.read(TOKEN_B))
        self.assertNotIn("watch_run", self.read(TOKEN_B, "startCoachSession"))

    def test_the_run_does_not_wait_on_an_intervals_permission(self):
        self.finish_a_run()
        limited = "synthetic-calendar-only"
        record_token_fingerprint(self.identity_db, token_fingerprint(limited, hmac_key=HMAC_KEY),
                                 self.owner, "intervals", scope_names=("CALENDAR:READ",))
        self.assertEqual("ended", self.read(limited)["watch_run"]["state"])

    def test_the_watch_run_changes_nothing_the_coach_counts_or_stores(self):
        before = self.read(TOKEN_A, "startCoachSession")
        state_before = self.read(TOKEN_A)
        self.finish_a_run()
        after = self.read(TOKEN_A, "startCoachSession")
        state_after = self.read(TOKEN_A)
        self.assertIsNone(before.pop("watch_run"))
        self.assertIsNotNone(after.pop("watch_run"))
        for answer in (before, after):
            answer["context"].pop("context_id")
        self.assertEqual(before, after)
        self.assertIsNone(state_before.pop("watch_run"))
        self.assertIsNotNone(state_after.pop("watch_run"))
        self.assertEqual(state_before, state_after)
        # A sample carries the session id and only a recap carries this key.
        for path in self.state_root.rglob("*"):
            if path.is_file():
                for trace in (b"fixture-1", b"stopped_or_walking"):
                    self.assertNotIn(trace, path.read_bytes(), path)

    def test_a_finished_run_is_held_a_day_then_dropped(self):
        wall, elapsed = [time.time()], [1000.0]
        self.gateway.live_runs = LiveRunStore(clock=lambda: wall[0], monotonic=lambda: elapsed[0])
        self.finish_a_run(start=int(wall[0]) - 20 * 60 - 30)
        wall[0] += RETENTION_SECONDS - 60
        elapsed[0] += RETENTION_SECONDS - 60
        self.assertEqual("ended", self.read(TOKEN_A)["watch_run"]["state"])
        wall[0] += 60
        elapsed[0] += 60
        self.assertIsNone(self.read(TOKEN_A)["watch_run"])
        self.assertIsNone(self.read(TOKEN_A, "startCoachSession")["watch_run"])

    def test_the_device_credential_opens_uploads_and_nothing_else(self):
        for bad in (None, TOKEN_A, self.mcp_bearer(TOKEN_A), self.watch + "x"):
            with self.subTest(bearer=bad):
                status, refused = self.call("POST", LIVE_RUN_PATH, token=bad, body=self.sample)
                self.assertEqual((401, "invalid_device_credential"), (status, refused["error"]))
        self.assertEqual(401, self.call("POST", MCP_PATH, token=self.watch,
                                        body=self.tool_rpc("getCoachState"))[0])
        self.assertEqual(400, self.call("POST", LIVE_RUN_PATH, token=self.watch,
                                        body={**self.sample, "owner_id": self.owner})[0])
        refusals = [line for line in self.log_handler.records if '"event": "watch_authentication"' in line]
        self.assertEqual(4, len(refusals), refusals)
        self.assertFalse(any(self.watch in line for line in self.log_handler.records))


class WatchRateLimitTests(WatchGatewayTestCase):
    def test_asking_for_codes_is_counted_per_source_address(self):
        here = {"X-Real-IP": "203.0.113.7"}
        for _ in range(10):
            self.start(headers=here)
        status, headers, refused = self.raw_post(WATCH_PAIRING_PATH, {"secret": SECRET}, headers=here)
        self.assertEqual((429, "rate_limited"), (status, refused["error"]))
        self.assertGreaterEqual(int(headers["Retry-After"]), 1)
        # Another address has its own count, and a header that is not an address is the
        # socket peer's count rather than a key of the caller's choosing.
        self.start(headers={"X-Real-IP": "2001:db8::7"})
        for _ in range(10):
            self.start(headers={"X-Real-IP": "not-an-address"})
        self.assertEqual(429, self.raw_post(WATCH_PAIRING_PATH, {"secret": SECRET},
                                            headers={"X-Real-IP": "127.0.0.1"})[0])

    def test_codes_have_a_ceiling_for_everyone_together(self):
        """A caller with many addresses still cannot grow the registry without end."""
        self.gateway._watch_start_ceiling = RateLimiter(3, 600)
        for index in range(3):
            self.start(headers={"X-Real-IP": f"198.51.100.{index + 1}"})
        status, headers, refused = self.raw_post(WATCH_PAIRING_PATH, {"secret": SECRET},
                                                 headers={"X-Real-IP": "203.0.113.200"})
        self.assertEqual((429, "rate_limited"), (status, refused["error"]))
        self.assertIn("Retry-After", headers)
        # Polls write no row and are not held to it.
        self.assertEqual({"status": "invalid"}, self.poll("ZZZZ2222"))

    def test_one_ipv6_subscriber_block_is_one_count(self):
        self.gateway._watch_address_limits["watch_pairing_start"] = RateLimiter(2, 600)
        for suffix in ("::1", "::2"):
            self.start(headers={"X-Real-IP": "2001:db8:1:2" + suffix})
        self.assertEqual(429, self.raw_post(WATCH_PAIRING_PATH, {"secret": SECRET},
                                            headers={"X-Real-IP": "2001:db8:1:2::3"})[0])
        self.start(headers={"X-Real-IP": "2001:db8:1:3::1"})

    def test_polling_and_uploads_are_counted_too(self):
        code = self.start()["code"]
        here = {"X-Real-IP": "198.51.100.4"}
        for _ in range(60):
            self.poll(code, headers=here)
        self.assertEqual(429, self.raw_post(WATCH_PAIRING_POLL_PATH, {"code": code, "secret": SECRET},
                                            headers=here)[0])
        watch = self.pair()
        for seq in range(1, 31):
            self.assertEqual(200, self.call("POST", LIVE_RUN_PATH, token=watch,
                                            body=running(seq, seq * 5000, int(time.time())))[0])
        status, headers, refused = self.raw_post(LIVE_RUN_PATH, running(31, 155000, int(time.time())), token=watch)
        self.assertEqual((429, "rate_limited"), (status, refused["error"]))
        self.assertIn("Retry-After", headers)

    def test_an_account_saying_codes_in_a_loop_is_stopped(self):
        for _ in range(10):
            self.assertEqual("code_not_accepted",
                             self.tool(TOKEN_A, "connectWatch", {"code": "ZZZZ2222"})["structuredContent"]["status"])
        refused = self.tool(TOKEN_A, "connectWatch", {"code": "ZZZZ2222"})
        self.assertTrue(refused.get("isError"), refused)
        self.assertIn("rate_limited", json.dumps(refused))
        # Another account is not held to this one's count.
        self.assertEqual("code_not_accepted",
                         self.tool(TOKEN_B, "connectWatch", {"code": "ZZZZ2222"})["structuredContent"]["status"])


class TrainingProductBoundaryTests(unittest.TestCase):
    """The Plugin entry pairs no watch: no route, no tool, no run in any read."""

    def gateway_for(self, product, health_data):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        config = GatewayConfig(
            state_root=Path(tmp.name).resolve(), token_hmac_key=HMAC_KEY, product=product,
            intervals_client_id=CLIENT_ID_VALUE, intervals_client_secret=CLIENT_SECRET_VALUE,
            health_data=health_data,
        )
        run_preflight(config)
        gateway = CoachGateway(config, fetch=FakeIntervals(), now=lambda: NOW)
        owner = lookup_or_create_owner(config.identity_db_path, "intervals", "synthetic-athlete")
        record_token_fingerprint(config.identity_db_path, token_fingerprint(TOKEN_A, hmac_key=HMAC_KEY),
                                 owner, "intervals", scope_names=("ACTIVITY:READ",))
        return gateway, owner

    def closed(self):
        return (("training", False), ("training", True), ("hybrid", False))

    def test_only_the_full_product_with_health_data_pairs_a_watch(self):
        for product, health_data in (*self.closed(), ("hybrid", True)):
            with self.subTest(product=product, health_data=health_data):
                gateway, _ = self.gateway_for(product, health_data)
                expected = (product, health_data) == ("hybrid", True)
                self.assertIs(expected, gateway.config.watch_pairing)
                self.assertIs(expected, "connectWatch" in gateway.surface.tools_by_name)

    def test_a_closed_deployment_answers_every_watch_route_404(self):
        for product, health_data in self.closed():
            with self.subTest(product=product, health_data=health_data):
                gateway, _ = self.gateway_for(product, health_data)
                server = CoachGatewayServer(("127.0.0.1", 0), CoachGatewayHandler, gateway=gateway)
                thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
                thread.start()
                try:
                    for path in WATCH_ROUTES:
                        request = urllib.request.Request(
                            "http://127.0.0.1:%d%s" % (server.server_address[1], path),
                            data=json.dumps({"secret": SECRET, "code": "K7P4QX9A"}).encode(), method="POST",
                            headers={"Content-Type": "application/json", "Authorization": "Bearer synthetic-watch"},
                        )
                        with self.assertRaises(urllib.error.HTTPError) as refused:
                            urllib.request.urlopen(request, timeout=10)
                        refused.exception.close()
                        self.assertEqual(404, refused.exception.code, path)
                finally:
                    server.shutdown()
                    server.server_close()
                    thread.join(5)

    def test_a_closed_deployment_refuses_the_pairing_act_called_directly(self):
        for product, health_data in self.closed():
            with self.subTest(product=product, health_data=health_data):
                gateway, owner = self.gateway_for(product, health_data)
                self.assertNotIn("connectWatch", gateway.surface.tools_by_name)
                for call in (lambda: gateway.route("watch_connect", owner, TOKEN_A, {"code": "K7P4QX9A"}),
                             lambda: gateway.start_watch_pairing({"secret": SECRET}),
                             lambda: gateway.poll_watch_pairing({"code": "K7P4QX9A", "secret": SECRET}),
                             lambda: gateway.live_run_writer("synthetic-watch")):
                    with self.assertRaises(GatewayError) as refused:
                        call()
                    self.assertEqual(404, refused.exception.status)

    def test_a_closed_read_never_returns_samples_its_store_holds(self):
        for product, health_data in self.closed():
            with self.subTest(product=product, health_data=health_data):
                gateway, owner = self.gateway_for(product, health_data)
                gateway.live_runs.ingest(owner, running(1, 5000, int(time.time())))
                for kind in ("state", "session"):
                    answer = gateway.route(kind, owner, TOKEN_A, {})
                    self.assertNotIn("watch_run", answer)
                    self.assertNotIn("fixture-1", json.dumps(answer))

    def test_the_plugin_catalogue_is_the_one_it_already_had(self):
        for health_data in (False, True):
            with self.subTest(health_data=health_data):
                served = product_scope.surface(health_data, product="training")
                self.assertNotIn("connectWatch", served.tools_by_name)
        self.assertEqual(
            "da469a84bfa7d3bddaacf7cb52f5911cadc03406cb192019b1a2655a793b0c93",
            product_scope.surface(False, product="training").tool_catalogue_sha256(),
        )


if __name__ == "__main__":
    unittest.main()
