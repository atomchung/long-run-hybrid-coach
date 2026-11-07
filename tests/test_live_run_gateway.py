"""Exercise the watch POST and the existing authenticated Coach MCP read over HTTP."""

import hashlib
import json
import tempfile
import threading
import time
import urllib.error
import urllib.request
import unittest
from dataclasses import replace
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

from garmin_coach_loop.gateway import (
    CoachGateway, CoachGatewayHandler, CoachGatewayServer, GatewayConfig, GatewayConfigError, GatewayError,
    LIVE_RUN_PATH, MCP_PATH, PILOT_ATHLETE_DIGEST_ENV, load_config, run_preflight,
)
from garmin_coach_loop.hosted import HostedClient, code_verifier
from garmin_coach_loop.identity import (
    lookup_or_create_owner, owner_count, owner_for_fingerprint, record_token_fingerprint, token_fingerprint,
)
from garmin_coach_loop.live_run import RETENTION_SECONDS, LiveRunStore
from tests.test_gateway import (
    CLIENT_ID_VALUE, CLIENT_SECRET_VALUE, HMAC_KEY, NOW, TOKEN_A, TOKEN_B, FakeIntervals, GatewayTestCase,
    publishable_plan,
)


def running(seq, timer_ms, observed_at, **updates):
    return {"session_id": "fixture-1", "seq": seq, "observed_at": observed_at, "state": "running",
            "heart_rate_bpm": 140, "cadence_rpm": 84, "speed_mps": 2.8, "timer_ms": timer_ms,
            "distance_m": timer_ms * 0.0028, "altitude_m": 30.0, "ascent_m": 4, "descent_m": 3,
            "power_w": None, "steps_done": 0, **updates}


class LiveRunGatewayTests(GatewayTestCase):
    watch = "synthetic-watch-writer-for-local-test-only"

    def setUp(self):
        super().setUp()
        self.owner = self.seed_owner(TOKEN_A, plan=publishable_plan())
        other = self.seed_owner(TOKEN_B, athlete_id="i2")
        self.grant_activity(TOKEN_B, other)
        self.grant_activity(TOKEN_A, self.owner)
        self.enabled = replace(self.config, live_run_owner=self.owner,
                               live_run_token_sha256=hashlib.sha256(self.watch.encode()).hexdigest())
        self.gateway.config = self.enabled
        self.sample = running(1, 5000, int(time.time()))

    def grant_activity(self, token, owner):
        record_token_fingerprint(self.identity_db, token_fingerprint(token, hmac_key=HMAC_KEY),
                                 owner, "intervals", scope_names=("ACTIVITY:READ",))

    def read(self, token, tool="getCoachState"):
        status, reply = self.call("POST", MCP_PATH, body=self.tool_rpc(tool),
                                  token=self.mcp_bearer(token))
        self.assertEqual(200, status)
        return reply["result"]["structuredContent"]

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
        self.assertNotIn("watch_run", self.read(TOKEN_B))
        self.assertNotIn("watch_run", self.read(TOKEN_B, "startCoachSession"))

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

    def test_the_full_product_keeps_every_scope_with_a_watch_configured(self):
        self.assertEqual(self.config.intervals_oauth_scopes, self.enabled.intervals_oauth_scopes)
        self.assertIn("CALENDAR:WRITE", self.enabled.intervals_oauth_scopes)

    def test_a_connection_without_activity_read_does_not_see_the_run(self):
        self.finish_a_run()
        limited = "synthetic-calendar-only"
        record_token_fingerprint(self.identity_db, token_fingerprint(limited, hmac_key=HMAC_KEY),
                                 self.owner, "intervals", scope_names=("CALENDAR:READ",))
        self.assertNotIn("watch_run", self.read(limited))
        self.assertNotIn("watch_run", self.read(limited, "startCoachSession"))

    def test_the_watch_credential_is_separate_and_the_path_closes_without_an_owner(self):
        for bad in (None, TOKEN_A, self.mcp_bearer(TOKEN_A)):
            self.assertEqual(401, self.call("POST", LIVE_RUN_PATH, token=bad, body=self.sample)[0])
        self.assertEqual(401, self.call("POST", MCP_PATH, token=self.watch,
                                        body=self.tool_rpc("getCoachState"))[0])
        self.assertEqual(400, self.call("POST", LIVE_RUN_PATH, token=self.watch,
                                        body={**self.sample, "owner_id": self.owner})[0])
        self.gateway.config = self.config
        self.assertEqual(404, self.call("POST", LIVE_RUN_PATH, token=self.watch, body=self.sample)[0])
        self.assertNotIn("watch_run", self.read(TOKEN_A))
        with self.assertRaises(GatewayConfigError):
            replace(self.enabled, health_data=False)
        with self.assertRaises(GatewayConfigError):
            replace(self.enabled, live_run_token_sha256=None)


class TrainingProductBoundaryTests(unittest.TestCase):
    """The Plugin entry holds no watch samples: none in, none out, whatever its store holds."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.config = GatewayConfig(
            state_root=Path(tmp.name).resolve(), token_hmac_key=HMAC_KEY, product="training",
            intervals_client_id=CLIENT_ID_VALUE, intervals_client_secret=CLIENT_SECRET_VALUE,
        )
        run_preflight(self.config)
        self.gateway = CoachGateway(self.config, fetch=FakeIntervals(), now=lambda: NOW)
        self.owner = lookup_or_create_owner(self.config.identity_db_path, "intervals", "synthetic-athlete")
        record_token_fingerprint(self.config.identity_db_path, token_fingerprint(TOKEN_A, hmac_key=HMAC_KEY),
                                 self.owner, "intervals", scope_names=("ACTIVITY:READ",))

    def test_a_training_deployment_cannot_be_given_a_watch(self):
        digest = hashlib.sha256(b"synthetic-watch").hexdigest()
        for health_data in (False, True):
            with self.subTest(health_data=health_data), self.assertRaises(GatewayConfigError):
                replace(self.config, health_data=health_data, live_run_owner=self.owner,
                        live_run_token_sha256=digest)

    def test_a_training_deployment_refuses_samples_over_http(self):
        server = CoachGatewayServer(("127.0.0.1", 0), CoachGatewayHandler, gateway=self.gateway)
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 5)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        request = urllib.request.Request(
            "http://127.0.0.1:%d%s" % (server.server_address[1], LIVE_RUN_PATH),
            data=json.dumps(running(1, 5000, int(time.time()))).encode(), method="POST",
            headers={"Content-Type": "application/json", "Authorization": "Bearer synthetic-watch"},
        )
        with self.assertRaises(urllib.error.HTTPError) as refused:
            urllib.request.urlopen(request, timeout=10)
        refused.exception.close()
        self.assertEqual(404, refused.exception.code)

    def test_a_training_read_never_returns_samples_its_store_holds(self):
        self.gateway.live_runs.ingest(self.owner, running(1, 5000, int(time.time())))
        for kind in ("state", "session"):
            with self.subTest(kind=kind):
                answer = self.gateway.route(kind, self.owner, TOKEN_A, {})
                self.assertNotIn("watch_run", answer)
                self.assertNotIn("fixture-1", json.dumps(answer))


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


if __name__ == "__main__":
    unittest.main()
