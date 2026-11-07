"""Exercise the watch POST and the existing authenticated Coach MCP read over HTTP."""

import concurrent.futures
import hashlib
import time
from dataclasses import replace
from urllib.parse import parse_qsl, urlsplit

from garmin_coach_loop.gateway import (
    GatewayConfigError, GatewayError, LIVE_RUN_PATH, MCP_PATH, PILOT_ATHLETE_DIGEST_ENV, load_config,
)
from garmin_coach_loop.hosted import HostedClient, code_verifier
from garmin_coach_loop.identity import (
    owner_count, owner_for_fingerprint, record_token_fingerprint, token_fingerprint,
)
from tests.test_gateway import GatewayTestCase, HMAC_KEY, TOKEN_A, TOKEN_B


class LiveRunGatewayTests(GatewayTestCase):
    watch = "synthetic-watch-writer-for-local-test-only"

    def setUp(self):
        super().setUp()
        self.owner = self.seed_owner(TOKEN_A)
        other = self.seed_owner(TOKEN_B, athlete_id="i2")
        self.grant_activity(TOKEN_B, other)
        self.grant_activity(TOKEN_A, self.owner)
        self.enabled = replace(self.config, live_run_owner=self.owner,
                               live_run_token_sha256=hashlib.sha256(self.watch.encode()).hexdigest())
        self.gateway.config = self.enabled
        self.sample = dict(session_id="fixture-1", seq=1, observed_at=int(time.time()),
                           state="running", heart_rate_bpm=130, cadence_rpm=81, speed_mps=2.5)

    def grant_activity(self, token, owner):
        record_token_fingerprint(self.identity_db, token_fingerprint(token, hmac_key=HMAC_KEY),
                                 owner, "intervals", scope_names=("ACTIVITY:READ",))

    def read(self, token, tool="getCoachState"):
        status, reply = self.call("POST", MCP_PATH, body=self.tool_rpc(tool),
                                  token=self.mcp_bearer(token))
        self.assertEqual(200, status)
        return reply["result"]["structuredContent"]

    def test_watch_sample_reaches_only_its_authenticated_coach(self):
        self.assertEqual({"status": "no_recent_sample"}, self.read(TOKEN_A)["live_run"])
        status, ack = self.call("POST", LIVE_RUN_PATH, token=self.watch, body=self.sample)
        self.assertEqual(200, status)
        self.assertEqual("accepted", ack["status"])
        self.assertNotIn("heart_rate_bpm", ack)
        live = self.read(TOKEN_A)["live_run"]
        self.assertEqual(("fresh", 81), (live["freshness"], live["cadence_rpm"]))
        self.assertNotIn("live_run", self.read(TOKEN_B))
        self.assertEqual([], self.fake.calls)

    def test_agent_can_follow_changes_without_a_prescribed_plan(self):
        for seq, state, cadence in [(1, "running", 81), (2, "running", 91), (3, "paused", None),
                                    (4, "running", 79), (5, "ended", None)]:
            with self.subTest(seq=seq):
                sample = {**self.sample, "state": state, "seq": seq, "cadence_rpm": cadence}
                self.assertEqual(200, self.call("POST", LIVE_RUN_PATH, token=self.watch, body=sample)[0])
                live = self.read(TOKEN_A)["live_run"]
                self.assertEqual((seq, state, cadence), (live["seq"], live["state"], live["cadence_rpm"]))
        self.assertEqual("ended", self.read(TOKEN_A, "startCoachSession")["live_run"]["state"])

    def held_config(self, seconds):
        self.gateway.config = replace(self.enabled, live_run_hold_seconds=seconds, live_run_brief="今天輕鬆跑")

    def test_a_repeat_read_waits_for_the_next_stretch_and_keeps_the_model_watching(self):
        self.held_config(0.4)
        running = {**self.sample, "timer_ms": 5000, "distance_m": 12.0, "altitude_m": 30.0}
        self.assertEqual(200, self.call("POST", LIVE_RUN_PATH, token=self.watch, body=running)[0])
        first = self.read(TOKEN_A)["live_run"]
        self.assertIn("再呼叫一次 getCoachState", first["companion"])
        self.assertEqual("今天輕鬆跑", first["today"])
        self.assertEqual(0.1, first["trend"]["elapsed_minutes"])
        started = time.monotonic()
        second = self.read(TOKEN_A)["live_run"]
        self.assertGreaterEqual(time.monotonic() - started, 0.3)
        self.assertIn("companion", second)
        self.assertNotIn("today", second)

    def test_passing_a_ten_minute_mark_asks_the_model_to_speak(self):
        self.held_config(0.01)
        early = {**self.sample, "timer_ms": 590_000, "distance_m": 1200.0, "altitude_m": 30.0}
        self.assertEqual(200, self.call("POST", LIVE_RUN_PATH, token=self.watch, body=early)[0])
        self.assertNotIn("milestone_minutes", self.read(TOKEN_A)["live_run"])
        later = {**early, "seq": 2, "timer_ms": 601_000}
        self.assertEqual(200, self.call("POST", LIVE_RUN_PATH, token=self.watch, body=later)[0])
        live = self.read(TOKEN_A)["live_run"]
        self.assertEqual(10, live["milestone_minutes"])
        self.assertIn("milestone_minutes", live["companion"])
        self.assertNotIn("milestone_minutes", self.read(TOKEN_A)["live_run"])

    def test_the_session_read_is_never_held_and_a_finished_run_ends_the_loop(self):
        self.held_config(30)
        self.assertEqual(200, self.call("POST", LIVE_RUN_PATH, token=self.watch, body=self.sample)[0])
        started = time.monotonic()
        self.read(TOKEN_A, "startCoachSession")
        self.read(TOKEN_A, "startCoachSession")
        ended = {**self.sample, "seq": 2, "state": "ended", "heart_rate_bpm": None, "cadence_rpm": None,
                 "speed_mps": None}
        self.assertEqual(200, self.call("POST", LIVE_RUN_PATH, token=self.watch, body=ended)[0])
        self.assertNotIn("companion", self.read(TOKEN_A)["live_run"])
        self.assertLess(time.monotonic() - started, 5)

    def test_the_watch_can_upload_while_a_read_is_held(self):
        self.held_config(0.8)
        self.assertEqual(200, self.call("POST", LIVE_RUN_PATH, token=self.watch, body=self.sample)[0])
        self.read(TOKEN_A)
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            held = pool.submit(self.read, TOKEN_A)
            time.sleep(0.1)
            started = time.monotonic()
            later = {**self.sample, "seq": 2, "heart_rate_bpm": 151}
            self.assertEqual(200, self.call("POST", LIVE_RUN_PATH, token=self.watch, body=later)[0])
            self.assertLess(time.monotonic() - started, 0.4)
            self.assertEqual(151, held.result(timeout=10)["live_run"]["heart_rate_bpm"])

    def test_a_deployment_without_a_hold_answers_every_read_at_once(self):
        self.assertEqual(200, self.call("POST", LIVE_RUN_PATH, token=self.watch, body=self.sample)[0])
        started = time.monotonic()
        for _ in range(3):
            self.assertNotIn("companion", self.read(TOKEN_A)["live_run"])
        self.assertLess(time.monotonic() - started, 5)

    def test_the_pilot_offers_new_clients_read_access_only(self):
        self.assertEqual(("ACTIVITY:READ",), self.enabled.intervals_oauth_scopes)
        self.assertNotEqual(("ACTIVITY:READ",), self.config.intervals_oauth_scopes)

    def test_old_data_and_missing_permission_are_not_current(self):
        old = {**self.sample, "observed_at": int(time.time()) - 22}
        self.assertEqual(200, self.call("POST", LIVE_RUN_PATH, token=self.watch, body=old)[0])
        live = self.read(TOKEN_A)["live_run"]
        self.assertEqual("stale", live["freshness"])
        self.assertIsNone(live["heart_rate_bpm"])
        limited = "synthetic-calendar-only"
        record_token_fingerprint(self.identity_db, token_fingerprint(limited, hmac_key=HMAC_KEY),
                                 self.owner, "intervals", scope_names=("CALENDAR:READ",))
        self.assertNotIn("live_run", self.read(limited))

    def test_separate_credentials_and_closed_pilot(self):
        for bad in (None, TOKEN_A, self.mcp_bearer(TOKEN_A)):
            self.assertEqual(401, self.call("POST", LIVE_RUN_PATH, token=bad, body=self.sample)[0])
        self.assertEqual(401, self.call("POST", MCP_PATH, token=self.watch,
                                        body=self.tool_rpc("getCoachState"))[0])
        self.assertEqual(400, self.call("POST", LIVE_RUN_PATH, token=self.watch,
                                        body={**self.sample, "owner_id": self.owner})[0])
        self.gateway.config = self.config
        self.assertEqual(404, self.call("POST", LIVE_RUN_PATH, token=self.watch, body=self.sample)[0])
        self.assertNotIn("live_run", self.read(TOKEN_A))
        with self.assertRaises(GatewayConfigError):
            replace(self.enabled, health_data=False)
        with self.assertRaises(GatewayConfigError):
            replace(self.enabled, live_run_token_sha256=None)


class PrivateAthletePilotTests(GatewayTestCase):
    allowed = "synthetic-private-athlete"
    denied = "synthetic-other-athlete"
    redirect = "http://127.0.0.1:8765/callback"

    def setUp(self):
        super().setUp()
        self.digest = hashlib.sha256(self.allowed.encode()).hexdigest()
        self.private = replace(self.config, pilot_athlete_sha256=self.digest)
        self.client = HostedClient(self.base_url)
        self.client_id = self.client.register(self.redirect)
        self.verifier = code_verifier()

    def browser_callback(self, athlete, token):
        url = self.client.authorization_url(client_id=self.client_id, redirect_uri=self.redirect,
                                            verifier=self.verifier, state="synthetic-client-state")
        request = self.gateway.start_authorization(dict(parse_qsl(urlsplit(url).query)),
                                                   base_url=self.base_url)
        state = dict(parse_qsl(urlsplit(request.location).query))["state"]
        self.fake.token_payload = {"athlete": {"id": athlete}, "access_token": token,
                                   "scope": "ACTIVITY:READ"}
        callback = self.gateway.complete_authorization({"state": state, "code": "synthetic-code"},
                                                       base_url=self.base_url)
        return dict(parse_qsl(urlsplit(callback.location).query))

    def exchange(self, code):
        return self.gateway.issue_access_token({
            "grant_type": "authorization_code", "client_id": self.client_id,
            "code": code, "code_verifier": self.verifier, "redirect_uri": self.redirect,
        }, base_url=self.base_url)

    def test_only_matching_provider_athlete_can_finish_oauth(self):
        self.gateway.config = self.private
        for different in (self.denied, self.allowed.upper(), self.allowed + " "):
            with self.subTest(athlete=different):
                reply = self.browser_callback(different, TOKEN_B)
                self.assertEqual({"state": "synthetic-client-state", "error": "access_denied"}, reply)
                self.assertEqual(0, owner_count(self.identity_db))
                self.assertIsNone(owner_for_fingerprint(self.identity_db,
                                  token_fingerprint(TOKEN_B, hmac_key=HMAC_KEY)))
        self.assertFalse(any(self.denied in line or TOKEN_B in line for line in self.log_handler.records))
        callback = self.browser_callback(self.allowed, TOKEN_A)
        bearer = self.exchange(callback["code"])["access_token"]
        self.assertEqual(1, owner_count(self.identity_db))
        self.assertEqual(200, self.call("POST", MCP_PATH, body=self.tool_rpc("getCoachState"), token=bearer)[0])

    def test_restriction_rechecks_old_bearers_and_outstanding_codes(self):
        old_bearer = self.exchange(self.browser_callback(self.denied, TOKEN_B)["code"])["access_token"]
        old_code = self.browser_callback(self.denied, TOKEN_B)["code"]
        owner = self.gateway.resolve_owner(TOKEN_B)
        watch = "synthetic-mismatched-watch"
        self.gateway.config = replace(self.private, live_run_owner=owner,
                                      live_run_token_sha256=hashlib.sha256(watch.encode()).hexdigest())
        self.assertEqual(401, self.call("POST", MCP_PATH, body=self.tool_rpc("getCoachState"), token=old_bearer)[0])
        with self.assertRaises(GatewayError) as error:
            self.exchange(old_code)
        self.assertEqual("invalid_grant", error.exception.code)
        with self.assertRaises(GatewayError) as error:
            self.gateway.live_run_writer(watch)
        self.assertEqual(403, error.exception.status)

    def test_private_config_is_opt_in_and_rejects_empty_or_noncanonical_digests(self):
        env = {"GARMIN_COACH_LOOP_GATEWAY_STATE_ROOT": str(self.state_root),
               "GARMIN_COACH_LOOP_TOKEN_HMAC_KEY": HMAC_KEY.decode(),
               "GARMIN_COACH_LOOP_INTERVALS_CLIENT_ID": "test",
               "GARMIN_COACH_LOOP_INTERVALS_CLIENT_SECRET": "test"}
        self.assertIsNone(load_config(env).pilot_athlete_sha256)
        self.assertEqual(self.digest, load_config({**env, PILOT_ATHLETE_DIGEST_ENV: self.digest}).pilot_athlete_sha256)
        for bad in ("", " " + self.digest, self.digest.upper(), "0" * 63):
            with self.subTest(bad=bad), self.assertRaises(GatewayConfigError):
                load_config({**env, PILOT_ATHLETE_DIGEST_ENV: bad})
        for options in ({"product": "training"}, {"health_data": False}):
            with self.subTest(options=options), self.assertRaises(GatewayConfigError):
                replace(self.private, **options)
