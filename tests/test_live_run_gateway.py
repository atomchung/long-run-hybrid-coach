"""Exercise the watch POST and the existing authenticated Coach MCP read over HTTP."""

import hashlib
import time
from dataclasses import replace

from garmin_coach_loop.gateway import GatewayConfigError, LIVE_RUN_PATH, MCP_PATH
from garmin_coach_loop.identity import record_token_fingerprint, token_fingerprint
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
