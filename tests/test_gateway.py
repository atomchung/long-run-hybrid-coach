from __future__ import annotations

import copy
import datetime as dt
import hashlib
import json
import logging
import os
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any
from unittest import mock

from garmin_coach_loop.gateway import (
    INTERVALS_TOKEN_URL,
    CoachGateway,
    CoachGatewayHandler,
    CoachGatewayServer,
    GatewayConfig,
    GatewayConfigError,
    load_config,
)
from garmin_coach_loop.identity import (
    lookup_or_create_owner,
    owner_for_fingerprint,
    record_token_fingerprint,
    token_fingerprint,
)
from garmin_coach_loop.store import (
    apply_decision,
    canonical_hash,
    default_state_dir,
    init_store,
    read_current_plan,
    resolve_state_dir,
    status_store,
)


ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "examples" / "garmin-coach-loop-28-day"

# Fixed instant inside the fixture plan's week (2026-08-10 .. 2026-08-16), so every
# generated context lands on a real planned day regardless of when the suite runs.
NOW = dt.datetime(2026, 8, 13, 0, 0, tzinfo=dt.timezone.utc)

# Synthetic throughout. Short on purpose: nothing here should look like credential
# material to a scanner, and nothing here has ever been real.
HMAC_KEY = b"unit-test-fingerprint-key-0000000"
TOKEN_A = "tok-alpha-1"
TOKEN_B = "tok-bravo-1"
UNKNOWN_TOKEN = "tok-nobody"
CLIENT_ID_VALUE = "test-client"
CLIENT_SECRET_VALUE = "test-only-not-real"


def load(name: str) -> dict[str, Any]:
    return json.loads((EXAMPLE / name).read_text(encoding="utf-8"))


def publishable_plan() -> dict[str, Any]:
    """The fixture plan with its two running sessions marked deliverable."""
    plan = load("plan-state-v1.json")
    for session in plan["week"]["sessions"]:
        if session["session_id"] in {"run-quality-01", "run-long-01"}:
            session["execution"]["publish_supported"] = True
    return plan


def _provider_step(step: dict[str, Any]) -> dict[str, Any]:
    """Mirror what Intervals echoes back for one delivered step."""
    if step["kind"] == "repeat":
        return {
            "reps": step["repetitions"],
            "steps": [_provider_step(child) for child in step["steps"]],
        }
    result: dict[str, Any] = {"text": step["name"]}
    duration = step["duration"]
    if duration["kind"] == "time":
        result["duration"] = duration["seconds"]
    else:
        result["distance"] = duration["meters"]
    target = step["target"]
    if target["kind"] == "pace":
        result["pace"] = {
            "start": target["low_seconds_per_km"],
            "end": target["high_seconds_per_km"],
            "units": "secs/km",
        }
    elif target["kind"] == "hr_ceiling":
        result["hr"] = {"start": 0, "end": target["ceiling_bpm"], "units": "bpm"}
    return result


def _http_error(url: str, status: int) -> urllib.error.HTTPError:
    """A synthetic upstream failure with its (empty) body already closed.

    urllib's error object is a file wrapper; leaving it open only produces a
    ResourceWarning, and nothing under test ever reads an upstream error body.
    """
    error = urllib.error.HTTPError(url, status, "denied", None, None)
    error.close()
    return error


class FakeIntervals:
    """One injected fetcher standing in for every intervals.icu call the gateway makes.

    Covering the token exchange, the two context reads and the whole delivery round trip
    in a single callable is what lets a test assert that a refused request reached the
    provider zero times.
    """

    def __init__(
        self,
        *,
        activities: list[dict[str, Any]] | None = None,
        wellness: list[dict[str, Any]] | None = None,
        token_payload: dict[str, Any] | None = None,
        plan: dict[str, Any] | None = None,
    ):
        self.activities = activities or []
        self.wellness = wellness or []
        self.token_payload = token_payload or {}
        self.calls: list[tuple[str, str]] = []
        self.authorizations: list[str] = []
        self.token_forms: list[dict[str, list[str]]] = []
        self.events: list[dict[str, Any]] = []
        self.bulk_calls: list[dict[str, Any]] = []
        self.read_status: int | None = None
        self.token_status: int | None = None
        self.steps_by_name: dict[str, list[dict[str, Any]]] = {}
        if plan is not None:
            self.steps_by_name = {
                session["structured_workout"]["name"]: session["structured_workout"]["steps"]
                for session in plan["week"]["sessions"]
                if isinstance(session.get("structured_workout"), dict)
            }

    def __call__(self, request: urllib.request.Request) -> bytes:
        url = request.full_url
        method = request.get_method()
        self.calls.append((method, url))
        header = request.get_header("Authorization")
        if header:
            self.authorizations.append(header)

        if url.startswith(INTERVALS_TOKEN_URL):
            self.token_forms.append(
                urllib.parse.parse_qs((request.data or b"").decode("utf-8"))
            )
            if self.token_status is not None:
                raise _http_error(url, self.token_status)
            return json.dumps(self.token_payload).encode("utf-8")

        if self.read_status is not None:
            raise _http_error(url, self.read_status)
        if "/activities?" in url:
            return json.dumps(self.activities).encode("utf-8")
        if "/wellness?" in url:
            return json.dumps(self.wellness).encode("utf-8")
        if method == "POST" and "/events/bulk" in url:
            return self._bulk_upsert(json.loads((request.data or b"[]").decode("utf-8")))
        if method == "GET" and "/events?" in url:
            day = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query).get("oldest", [""])[0]
            return json.dumps(
                [
                    event
                    for event in self.events
                    if str(event.get("start_date_local", ""))[:10] == day
                ]
            ).encode("utf-8")
        if method == "GET" and "/events/" in url:
            event_id = urllib.parse.urlsplit(url).path.rsplit("/", 1)[-1]
            stored = next(
                (event for event in self.events if str(event["id"]) == event_id), None
            )
            if stored is None:
                raise _http_error(url, 404)
            return json.dumps(self._readback(stored)).encode("utf-8")
        raise AssertionError(f"unexpected intervals URL in test: {method} {url}")

    def _bulk_upsert(self, payload: list[dict[str, Any]]) -> bytes:
        event = payload[0]
        self.bulk_calls.append(copy.deepcopy(event))
        existing = next(
            (item for item in self.events if item.get("external_id") == event["external_id"]),
            None,
        )
        event_id = str(existing["id"]) if existing else str(9000 + len(self.bulk_calls))
        self.events = [
            item for item in self.events if item.get("external_id") != event["external_id"]
        ]
        stored = {"id": event_id, **event}
        self.events.append(stored)
        return json.dumps([stored]).encode("utf-8")

    def _readback(self, stored: dict[str, Any]) -> dict[str, Any]:
        result = {**stored, "id": int(stored["id"])}
        steps = self.steps_by_name.get(str(stored.get("name")))
        if steps is not None and "workout_doc" not in result:
            result["workout_doc"] = {"steps": [_provider_step(step) for step in steps]}
        return result


class _RecordingHandler(logging.Handler):
    def __init__(self):
        super().__init__()
        self.records: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record.getMessage())


class GatewayTestCase(unittest.TestCase):
    """A real loopback server over an injected provider -- no external network anywhere."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.state_root = Path(self._tmp.name).resolve()
        self.identity_db = self.state_root / "identity.db"
        self.fake = FakeIntervals(plan=publishable_plan())
        self.config = GatewayConfig(
            state_root=self.state_root,
            token_hmac_key=HMAC_KEY,
            intervals_client_id=CLIENT_ID_VALUE,
            intervals_client_secret=CLIENT_SECRET_VALUE,
        )
        self.gateway = CoachGateway(self.config, fetch=self.fake, now=lambda: NOW)
        self.server = CoachGatewayServer(
            ("127.0.0.1", 0), CoachGatewayHandler, gateway=self.gateway
        )
        self.base_url = "http://127.0.0.1:%d" % self.server.server_address[1]
        # A short poll interval only affects how fast shutdown() is noticed in teardown.
        self._thread = threading.Thread(
            target=self.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
        )
        self._thread.start()

        self.log_handler = _RecordingHandler()
        self.logger = logging.getLogger("garmin_coach_loop.gateway")
        self._previous_level = self.logger.level
        self.logger.setLevel(logging.DEBUG)
        self.logger.addHandler(self.log_handler)

    def tearDown(self):
        self.logger.removeHandler(self.log_handler)
        self.logger.setLevel(self._previous_level)
        self.server.shutdown()
        self.server.server_close()
        self._thread.join(timeout=5)
        self._tmp.cleanup()

    # -- helpers ----------------------------------------------------------------------

    def call(
        self,
        method: str,
        path: str,
        *,
        body: Any = None,
        raw: bytes | None = None,
        token: str | None = None,
        content_type: str = "application/json",
    ) -> tuple[int, Any]:
        data = raw if raw is not None else (None if body is None else json.dumps(body).encode())
        request = urllib.request.Request(self.base_url + path, data=data, method=method)
        if data is not None:
            request.add_header("Content-Type", content_type)
        if token is not None:
            request.add_header("Authorization", "Bearer " + token)
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, json.loads(response.read() or b"{}")
        except urllib.error.HTTPError as exc:
            with exc:
                return exc.code, json.loads(exc.read() or b"{}")

    def seed_owner(
        self, token: str, *, athlete_id: str = "i1", plan: dict[str, Any] | None = None
    ) -> str:
        owner_id = lookup_or_create_owner(self.identity_db, "intervals", athlete_id)
        record_token_fingerprint(
            self.identity_db,
            token_fingerprint(token, hmac_key=HMAC_KEY),
            owner_id,
            "intervals",
        )
        if plan is not None:
            init_store(resolve_state_dir(owner_id, state_root=self.state_root), plan)
        return owner_id

    def owner_dir(self, owner_id: str) -> Path:
        return resolve_state_dir(owner_id, state_root=self.state_root)

    def snapshot(self, state_dir: Path) -> dict[str, str]:
        return {
            str(path.relative_to(state_dir)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(state_dir.rglob("*"))
            if path.is_file()
        }


# --------------------------------------------------------------------------------------
# OAuth token proxy
# --------------------------------------------------------------------------------------


class GatewayOAuthProxyTests(GatewayTestCase):
    def _exchange(self, **form: str) -> tuple[int, Any]:
        return self.call(
            "POST",
            "/oauth/intervals/token",
            raw=urllib.parse.urlencode(form).encode("utf-8"),
            content_type="application/x-www-form-urlencoded",
        )

    def test_authorization_code_exchange_registers_the_athlete_and_returns_only_oauth_fields(self):
        self.fake.token_payload = {
            "token_type": "Bearer",
            "access_token": TOKEN_A,
            "scope": "ACTIVITY:READ",
            "athlete": {"id": "i1", "name": "Fixture Athlete"},
        }

        status, payload = self._exchange(grant_type="authorization_code", code="c1")

        self.assertEqual(200, status)
        self.assertEqual({"token_type", "access_token", "scope"}, set(payload))
        self.assertEqual(TOKEN_A, payload["access_token"])
        self.assertEqual("ACTIVITY:READ", payload["scope"])

        # The exchange happened server-side, with our own client credentials.
        form = self.fake.token_forms[0]
        self.assertEqual(["c1"], form["code"])
        self.assertEqual([CLIENT_ID_VALUE], form["client_id"])
        self.assertEqual([CLIENT_SECRET_VALUE], form["client_secret"])

        owner_id = owner_for_fingerprint(
            self.identity_db, token_fingerprint(TOKEN_A, hmac_key=HMAC_KEY)
        )
        self.assertIsNotNone(owner_id)
        self.assertNotIn(TOKEN_A.encode("utf-8"), self.identity_db.read_bytes())

    def test_reauthorizing_the_same_athlete_keeps_the_same_owner(self):
        self.fake.token_payload = {
            "access_token": TOKEN_A,
            "scope": "ACTIVITY:READ",
            "athlete": {"id": "i1"},
        }
        self._exchange(grant_type="authorization_code", code="c1")
        first = owner_for_fingerprint(
            self.identity_db, token_fingerprint(TOKEN_A, hmac_key=HMAC_KEY)
        )

        self.fake.token_payload = {
            "access_token": TOKEN_B,
            "scope": "ACTIVITY:READ",
            "athlete": {"id": "i1"},
        }
        self._exchange(grant_type="authorization_code", code="c2")

        second = owner_for_fingerprint(
            self.identity_db, token_fingerprint(TOKEN_B, hmac_key=HMAC_KEY)
        )
        self.assertEqual(first, second)
        # The superseded token stops authenticating, mirroring what Intervals does to it.
        self.assertIsNone(
            owner_for_fingerprint(
                self.identity_db, token_fingerprint(TOKEN_A, hmac_key=HMAC_KEY)
            )
        )

    def test_refresh_token_grant_is_refused_because_intervals_issues_none(self):
        # The value is irrelevant and deliberately trivial: the grant type alone decides.
        status, payload = self._exchange(grant_type="refresh_token", refresh_token="r1")
        self.assertEqual(400, status)
        self.assertEqual({"error": "invalid_grant"}, payload)
        self.assertEqual([], self.fake.calls)

    def test_unsupported_grant_is_refused_before_any_upstream_call(self):
        status, payload = self._exchange(grant_type="client_credentials")
        self.assertEqual(400, status)
        self.assertEqual({"error": "unsupported_grant_type"}, payload)
        self.assertEqual([], self.fake.calls)

    def test_upstream_failure_returns_a_generic_error_and_leaks_nothing(self):
        self.fake.token_status = 400
        status, payload = self._exchange(grant_type="authorization_code", code="c1")

        self.assertEqual(502, status)
        self.assertEqual({"error": "server_error"}, payload)
        blob = json.dumps(payload) + " ".join(self.log_handler.records)
        for secret in (CLIENT_SECRET_VALUE, "c1", TOKEN_A):
            self.assertNotIn(secret, blob)
        self.assertFalse(self.identity_db.exists())

    def test_upstream_response_without_an_athlete_identity_is_refused(self):
        # No athlete id means no way to say which store the token may open.
        self.fake.token_payload = {"access_token": TOKEN_A, "scope": "ACTIVITY:READ"}
        status, payload = self._exchange(grant_type="authorization_code", code="c1")
        self.assertEqual(502, status)
        self.assertEqual({"error": "server_error"}, payload)
        self.assertFalse(self.identity_db.exists())


# --------------------------------------------------------------------------------------
# Identity boundary
# --------------------------------------------------------------------------------------


class GatewayIdentityBoundaryTests(GatewayTestCase):
    def test_unknown_token_is_refused_before_any_provider_or_state_read(self):
        status, payload = self.call("POST", "/v1/coach/session", body={}, token=UNKNOWN_TOKEN)

        self.assertEqual(401, status)
        self.assertEqual({"status": "blocked", "error": "unauthorized"}, payload)
        self.assertEqual([], self.fake.calls)
        self.assertFalse((self.state_root / "owners").exists())

    def test_missing_authorization_header_is_refused(self):
        status, payload = self.call("POST", "/v1/coach/session", body={})
        self.assertEqual(401, status)
        self.assertEqual({"status": "blocked", "error": "unauthorized"}, payload)
        self.assertEqual([], self.fake.calls)

    def test_two_athletes_get_disjoint_owners_state_dirs_and_answers(self):
        other_plan = copy.deepcopy(publishable_plan())
        other_plan["plan_id"] = "fixture-plan-002"
        owner_a = self.seed_owner(TOKEN_A, athlete_id="i1", plan=publishable_plan())
        owner_b = self.seed_owner(TOKEN_B, athlete_id="i2", plan=other_plan)

        self.assertNotEqual(owner_a, owner_b)
        self.assertNotEqual(self.owner_dir(owner_a), self.owner_dir(owner_b))

        # Even with the single-user CLI variable pointed straight at B's store, a request
        # authenticated as A can only ever reach A's own state.
        with mock.patch.dict(
            os.environ, {"GARMIN_COACH_LOOP_HOME": str(self.owner_dir(owner_b))}
        ):
            self.assertEqual(self.owner_dir(owner_b), default_state_dir())  # control
            status, payload = self.call("POST", "/v1/coach/session", body={}, token=TOKEN_A)

        self.assertEqual(200, status)
        self.assertEqual("fixture-plan-001", payload["plan_state"]["plan_id"])
        self.assertEqual(
            "fixture-plan-002", read_current_plan(self.owner_dir(owner_b))["plan_id"]
        )

    def test_owner_without_a_store_gets_an_explicit_answer_and_no_store_is_created(self):
        owner_id = self.seed_owner(TOKEN_A)
        status, payload = self.call("POST", "/v1/coach/session", body={}, token=TOKEN_A)

        self.assertEqual(200, status)
        self.assertEqual("no_plan_state", payload["status"])
        self.assertFalse(payload["plan_state"]["present"])
        self.assertIsNone(payload["plan_state"]["current_plan"])
        self.assertEqual([], self.fake.calls)
        self.assertFalse(self.owner_dir(owner_id).exists())


# --------------------------------------------------------------------------------------
# Session
# --------------------------------------------------------------------------------------


class GatewaySessionTests(GatewayTestCase):
    def setUp(self):
        super().setUp()
        self.owner_id = self.seed_owner(TOKEN_A, plan=publishable_plan())
        self.state_dir = self.owner_dir(self.owner_id)

    def test_new_session_reads_the_existing_goal_and_plan(self):
        status, payload = self.call("POST", "/v1/coach/session", body={}, token=TOKEN_A)

        self.assertEqual(200, status)
        self.assertEqual("passed", payload["status"])
        self.assertEqual("fixture-plan-001", payload["plan_state"]["plan_id"])
        self.assertEqual(1, payload["plan_state"]["plan_version"])
        self.assertEqual(
            publishable_plan()["goal"], payload["plan_state"]["current_plan"]["goal"]
        )
        self.assertEqual(
            "fixture-plan-001", payload["context"]["goal_context"]["plan_id"]
        )
        self.assertEqual("passed", payload["validation"]["status"])
        self.assertEqual("intervals_accepted", payload["delivery"]["max_delivery_state"])
        self.assertEqual("passed", payload["reconciliation"]["status"])

        # The provider was read with this request's own bearer token against athlete 0.
        self.assertTrue(all(header.startswith("Bearer ") for header in self.fake.authorizations))
        self.assertTrue(all("/athlete/0/" in url for _, url in self.fake.calls))

    def test_session_reports_observable_delivery_state_only(self):
        _, payload = self.call("POST", "/v1/coach/session", body={}, token=TOKEN_A)
        states = {entry["delivery_state"] for entry in payload["delivery"]["sessions"]}
        self.assertEqual({"not_published"}, states)
        self.assertNotIn("garmin", json.dumps(payload["delivery"]).lower())

    def test_revoked_token_fails_explicitly_and_leaves_plan_state_untouched(self):
        before = self.snapshot(self.state_dir)
        self.fake.read_status = 401

        status, payload = self.call("POST", "/v1/coach/session", body={}, token=TOKEN_A)

        self.assertEqual(502, status)
        self.assertEqual("provider_error", payload["error"])
        self.assertEqual("blocked", payload["status"])
        self.assertEqual(before, self.snapshot(self.state_dir))
        self.assertEqual(1, read_current_plan(self.state_dir)["current_version"])

    def test_malformed_session_input_is_a_request_error_not_a_provider_error(self):
        status, payload = self.call(
            "POST", "/v1/coach/session", body={"timezone": "Nowhere/Nothing"}, token=TOKEN_A
        )
        self.assertEqual(400, status)
        self.assertEqual("invalid_request", payload["error"])
        self.assertEqual([], self.fake.calls)


# --------------------------------------------------------------------------------------
# Decisions
# --------------------------------------------------------------------------------------


class GatewayDecisionTests(GatewayTestCase):
    def setUp(self):
        super().setUp()
        self.before = load("plan-state-v1.json")
        self.after = load("plan-state-v2-day-4.json")
        self.event = load("decision-event-day-4.json")
        self.context = load("coach-context-day-4.json")
        self.owner_id = self.seed_owner(TOKEN_A, plan=self.before)
        self.state_dir = self.owner_dir(self.owner_id)

    def bundle(self, **overrides: Any) -> dict[str, Any]:
        body = {
            "plan_id": self.before["plan_id"],
            "plan_version": self.before["version"],
            "context": self.context,
            "after_plan": self.after,
            "decision_event": self.event,
        }
        body.update(overrides)
        return body

    def prepare(self, **overrides: Any) -> tuple[int, Any]:
        return self.call(
            "POST", "/v1/coach/decision/prepare", body=self.bundle(**overrides), token=TOKEN_A
        )

    def test_prepare_returns_the_material_diff_and_writes_nothing(self):
        before_files = self.snapshot(self.state_dir)

        status, payload = self.prepare()

        self.assertEqual(200, status)
        self.assertEqual("passed", payload["status"])
        self.assertEqual(1, payload["base_version"])
        self.assertEqual(2, payload["resulting_version"])
        self.assertEqual(64, len(payload["proposal_hash"]))
        touched = {entry["session_id"] for entry in payload["diff"]["sessions_modified"]}
        self.assertEqual({"run-quality-01"}, touched)
        self.assertEqual([], payload["diff"]["sessions_added"])
        self.assertEqual([], payload["diff"]["sessions_removed"])
        self.assertEqual("passed", payload["validation"]["status"])
        self.assertEqual(before_files, self.snapshot(self.state_dir))

    def test_apply_commits_the_prepared_bundle_and_reports_the_new_version(self):
        _, prepared = self.prepare()

        status, payload = self.call(
            "POST",
            "/v1/coach/decision/apply",
            body=self.bundle(proposal_hash=prepared["proposal_hash"]),
            token=TOKEN_A,
        )

        self.assertEqual(200, status)
        self.assertEqual(2, payload["plan_version"])
        self.assertEqual(self.event["event_id"], payload["event_id"])
        self.assertFalse(payload["idempotent_replay"])
        self.assertEqual(2, status_store(self.state_dir)["current_version"])

    def test_a_second_session_call_returns_the_applied_version(self):
        _, prepared = self.prepare()
        self.call(
            "POST",
            "/v1/coach/decision/apply",
            body=self.bundle(proposal_hash=prepared["proposal_hash"]),
            token=TOKEN_A,
        )

        status, payload = self.call("POST", "/v1/coach/session", body={}, token=TOKEN_A)

        self.assertEqual(200, status)
        self.assertEqual(2, payload["plan_state"]["plan_version"])
        self.assertEqual(2, payload["context"]["goal_context"]["plan_version"])

    def test_retrying_the_same_apply_returns_the_current_version_instead_of_failing(self):
        _, prepared = self.prepare()
        body = self.bundle(proposal_hash=prepared["proposal_hash"])
        self.call("POST", "/v1/coach/decision/apply", body=body, token=TOKEN_A)

        status, payload = self.call("POST", "/v1/coach/decision/apply", body=body, token=TOKEN_A)

        self.assertEqual(200, status)
        self.assertTrue(payload["idempotent_replay"])
        self.assertEqual(2, payload["plan_version"])
        self.assertEqual(2, status_store(self.state_dir)["current_version"])
        self.assertEqual(2, len(list((self.state_dir / "commits").iterdir())))

    def test_stale_plan_version_on_apply_fails_closed_without_writing(self):
        before_files = self.snapshot(self.state_dir)
        stale = self.bundle(plan_version=99)
        self.assertEqual(409, self.prepare(plan_version=99)[0])
        # The proposal hash binds the base version, so recompute it for the stale claim
        # rather than letting a hash mismatch mask the version check under test.
        stale["proposal_hash"] = canonical_hash(
            {
                "plan_id": self.before["plan_id"],
                "base_version": 99,
                "after_plan": self.after,
                "decision_event": self.event,
            }
        )

        status, payload = self.call(
            "POST", "/v1/coach/decision/apply", body=stale, token=TOKEN_A
        )

        self.assertEqual(409, status)
        self.assertEqual("stale_plan_version", payload["error"])
        self.assertEqual(1, payload["current_plan_version"])
        self.assertEqual(before_files, self.snapshot(self.state_dir))

    def test_wrong_proposal_hash_on_apply_fails_closed_without_writing(self):
        before_files = self.snapshot(self.state_dir)

        status, payload = self.call(
            "POST",
            "/v1/coach/decision/apply",
            body=self.bundle(proposal_hash="f" * 64),
            token=TOKEN_A,
        )

        self.assertEqual(409, status)
        self.assertEqual("proposal_hash_mismatch", payload["error"])
        self.assertEqual(before_files, self.snapshot(self.state_dir))

    def test_an_invalid_bundle_is_blocked_by_the_unweakened_validator(self):
        flagged = copy.deepcopy(self.context)
        flagged["constraints"]["red_flags"]["chest_pain"] = True
        before_files = self.snapshot(self.state_dir)

        status, payload = self.prepare(context=flagged)

        self.assertEqual(422, status)
        self.assertEqual("validation_failed", payload["error"])
        self.assertTrue(payload["validation"]["errors"])
        self.assertEqual(before_files, self.snapshot(self.state_dir))

    def test_prepare_against_another_plan_id_is_refused(self):
        status, payload = self.prepare(plan_id="somebody-elses-plan")
        self.assertEqual(409, status)
        self.assertEqual("plan_mismatch", payload["error"])
        self.assertEqual("fixture-plan-001", payload["current_plan_id"])


# --------------------------------------------------------------------------------------
# Delivery
# --------------------------------------------------------------------------------------


class GatewayDeliveryTests(GatewayTestCase):
    def setUp(self):
        super().setUp()
        self.plan = publishable_plan()
        self.owner_id = self.seed_owner(TOKEN_A, plan=self.plan)
        self.state_dir = self.owner_dir(self.owner_id)

    def prepare_set(self, session_ids: list[str] | None = None) -> dict[str, Any]:
        status, payload = self.call(
            "POST",
            "/v1/coach/delivery/prepare",
            body={
                "plan_id": self.plan["plan_id"],
                "plan_version": self.plan["version"],
                "session_ids": session_ids or ["run-quality-01", "run-long-01"],
            },
            token=TOKEN_A,
        )
        self.assertEqual(200, status, payload)
        return payload

    def test_prepare_previews_the_selected_sessions_without_writing(self):
        before_files = self.snapshot(self.state_dir)

        payload = self.prepare_set()

        self.assertTrue(payload["confirmation_required"])
        self.assertEqual(
            ["run-quality-01", "run-long-01"],
            [item["session_id"] for item in payload["preview"]],
        )
        self.assertEqual(
            payload["proposal_hash"], payload["delivery_set"]["proposal_hash"]
        )
        self.assertEqual([], self.fake.bulk_calls)
        self.assertEqual(before_files, self.snapshot(self.state_dir))

    def test_one_confirmation_publishes_every_selected_workout(self):
        prepared = self.prepare_set()

        status, payload = self.call(
            "POST",
            "/v1/coach/delivery/publish",
            body={
                "delivery_set": prepared["delivery_set"],
                "proposal_hash": prepared["proposal_hash"],
                "confirmed": True,
            },
            token=TOKEN_A,
        )

        self.assertEqual(200, status, payload)
        self.assertEqual("intervals_accepted", payload["delivery_state"])
        self.assertEqual("intervals_accepted", payload["max_delivery_state"])
        self.assertEqual(2, len(payload["delivered"]))
        self.assertEqual(
            {"run-quality-01", "run-long-01"},
            {item["session_id"] for item in payload["delivered"]},
        )
        self.assertEqual(2, len(self.fake.bulk_calls))
        self.assertNotIn("garmin", json.dumps(payload).lower())

        current = read_current_plan(self.state_dir)["current_plan"]
        delivered = {
            session["session_id"]: session["execution"]
            for session in current["week"]["sessions"]
            if session["session_id"] in {"run-quality-01", "run-long-01"}
        }
        for execution in delivered.values():
            self.assertEqual("intervals_accepted", execution["delivery_state"])
            self.assertTrue(execution["external_id"])
        self.assertEqual(2, current["version"])
        self.assertEqual("passed", status_store(self.state_dir)["status"])

    def test_publishing_without_confirmation_is_refused(self):
        prepared = self.prepare_set()

        status, payload = self.call(
            "POST",
            "/v1/coach/delivery/publish",
            body={
                "delivery_set": prepared["delivery_set"],
                "proposal_hash": prepared["proposal_hash"],
            },
            token=TOKEN_A,
        )

        self.assertEqual(409, status)
        self.assertEqual("confirmation_required", payload["error"])
        self.assertEqual([], self.fake.bulk_calls)

    def test_workout_content_changed_after_the_preview_fails_closed(self):
        prepared = self.prepare_set(["run-quality-01"])
        tampered = copy.deepcopy(prepared["delivery_set"])
        tampered["items"][0]["workout"]["steps"][0]["duration"]["seconds"] = 60

        status, payload = self.call(
            "POST",
            "/v1/coach/delivery/publish",
            body={
                "delivery_set": tampered,
                "proposal_hash": prepared["proposal_hash"],
                "confirmed": True,
            },
            token=TOKEN_A,
        )

        self.assertEqual(409, status)
        self.assertEqual("delivery_blocked", payload["error"])
        self.assertEqual([], self.fake.bulk_calls)
        self.assertEqual(1, read_current_plan(self.state_dir)["current_version"])

    def test_a_plan_that_moved_after_the_preview_blocks_delivery(self):
        prepared = self.prepare_set(["run-quality-01"])
        # A real coaching decision lands between preview and publish.
        after = load("plan-state-v2-day-4.json")
        for session in after["week"]["sessions"]:
            if session["session_id"] in {"run-quality-01", "run-long-01"}:
                session["execution"]["publish_supported"] = True
        apply_decision(
            self.state_dir,
            context=load("coach-context-day-4.json"),
            after=after,
            event=load("decision-event-day-4.json"),
        )

        status, payload = self.call(
            "POST",
            "/v1/coach/delivery/publish",
            body={
                "delivery_set": prepared["delivery_set"],
                "proposal_hash": prepared["proposal_hash"],
                "confirmed": True,
            },
            token=TOKEN_A,
        )

        self.assertEqual(409, status)
        self.assertEqual("delivery_blocked", payload["error"])
        self.assertIn("version changed", payload["detail"])
        self.assertEqual([], self.fake.bulk_calls)

    def test_delivery_prepare_refuses_a_session_outside_the_current_plan(self):
        status, payload = self.call(
            "POST",
            "/v1/coach/delivery/prepare",
            body={
                "plan_id": self.plan["plan_id"],
                "plan_version": self.plan["version"],
                "session_ids": ["not-in-this-plan"],
            },
            token=TOKEN_A,
        )
        self.assertEqual(409, status)
        self.assertEqual("delivery_blocked", payload["error"])


# --------------------------------------------------------------------------------------
# HTTP surface, configuration and log hygiene
# --------------------------------------------------------------------------------------


class GatewayHttpSurfaceTests(GatewayTestCase):
    def test_health_needs_no_token_and_touches_nothing(self):
        status, payload = self.call("GET", "/healthz")
        self.assertEqual(200, status)
        self.assertEqual({"status": "ok", "api_version": "1.0"}, payload)
        self.assertEqual([], self.fake.calls)
        self.assertFalse((self.state_root / "owners").exists())

    def test_unknown_path_and_wrong_method_are_refused_without_authentication(self):
        self.assertEqual(
            (404, {"status": "blocked", "error": "not_found"}), self.call("GET", "/nope")
        )
        status, payload = self.call("GET", "/v1/coach/session", token=TOKEN_A)
        self.assertEqual(405, status)
        self.assertEqual("method_not_allowed", payload["error"])

    def test_oversized_and_wrongly_typed_bodies_are_refused(self):
        self.seed_owner(TOKEN_A, plan=publishable_plan())
        status, payload = self.call(
            "POST", "/v1/coach/session", raw=b"x" * (1024 * 1024 + 1), token=TOKEN_A
        )
        self.assertEqual(413, status)
        self.assertEqual("payload_too_large", payload["error"])

        status, payload = self.call(
            "POST",
            "/v1/coach/session",
            raw=b"grant_type=x",
            content_type="application/x-www-form-urlencoded",
            token=TOKEN_A,
        )
        self.assertEqual(415, status)
        self.assertEqual("unsupported_media_type", payload["error"])
        self.assertEqual([], self.fake.calls)

    def test_logs_and_error_bodies_carry_no_credential_material(self):
        owner_id = self.seed_owner(TOKEN_A, plan=publishable_plan())
        bodies = [
            self.call("GET", "/healthz")[1],
            self.call("POST", "/v1/coach/session", body={}, token=UNKNOWN_TOKEN)[1],
            self.call("POST", "/v1/coach/decision/prepare", body={}, token=TOKEN_A)[1],
            self.call(
                "POST", "/v1/coach/delivery/prepare", body={"plan_id": "x"}, token=TOKEN_A
            )[1],
        ]

        logged = "\n".join(self.log_handler.records)
        self.assertTrue(logged)
        for secret in (TOKEN_A, TOKEN_B, UNKNOWN_TOKEN, CLIENT_SECRET_VALUE):
            self.assertNotIn(secret, logged)
            self.assertNotIn(secret, json.dumps(bodies))
        self.assertNotIn(HMAC_KEY.decode("ascii"), logged)
        # Requests are still traceable: method, path, status and owner, nothing else.
        self.assertIn("POST /v1/coach/session -> 401 owner=-", logged)
        self.assertIn(f"owner={owner_id}", logged)


class GatewayConfigurationTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name).resolve()
        self.env = {
            "GARMIN_COACH_LOOP_GATEWAY_STATE_ROOT": str(self.root),
            "GARMIN_COACH_LOOP_TOKEN_HMAC_KEY": HMAC_KEY.decode("ascii"),
            "GARMIN_COACH_LOOP_INTERVALS_CLIENT_ID": CLIENT_ID_VALUE,
            "GARMIN_COACH_LOOP_INTERVALS_CLIENT_SECRET": CLIENT_SECRET_VALUE,
        }

    def tearDown(self):
        self._tmp.cleanup()

    def test_complete_configuration_loads(self):
        config = load_config(self.env, host="127.0.0.1", port=9)
        self.assertEqual(self.root, config.state_root)
        self.assertEqual(self.root / "identity.db", config.identity_db_path)
        self.assertEqual(9, config.port)

    def test_each_missing_variable_is_named_without_its_value(self):
        for name in self.env:
            with self.subTest(missing=name):
                partial = {key: value for key, value in self.env.items() if key != name}
                with self.assertRaises(GatewayConfigError) as caught:
                    load_config(partial)
                message = str(caught.exception)
                self.assertIn(name, message)
                for value in self.env.values():
                    self.assertNotIn(value, message)

    def test_the_single_user_home_variable_is_never_a_fallback(self):
        partial = {
            key: value
            for key, value in self.env.items()
            if key != "GARMIN_COACH_LOOP_GATEWAY_STATE_ROOT"
        }
        partial["GARMIN_COACH_LOOP_HOME"] = str(self.root)
        with self.assertRaises(GatewayConfigError):
            load_config(partial)

    def test_a_weak_fingerprint_key_is_refused(self):
        weak = dict(self.env, GARMIN_COACH_LOOP_TOKEN_HMAC_KEY="short")
        with self.assertRaises(GatewayConfigError):
            load_config(weak)

    def test_a_state_root_inside_the_repository_is_refused(self):
        inside = dict(
            self.env,
            GARMIN_COACH_LOOP_GATEWAY_STATE_ROOT=str(ROOT / "gateway-state"),
        )
        with self.assertRaises(GatewayConfigError):
            load_config(inside)
        self.assertFalse((ROOT / "gateway-state").exists())

    def test_the_cli_refuses_to_serve_without_configuration(self):
        environment = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith("GARMIN_COACH_LOOP_")
        }
        result = subprocess.run(
            [sys.executable, "-m", "garmin_coach_loop.cli", "serve-gateway"],
            cwd=ROOT,
            env=environment,
            check=False,
            capture_output=True,
            text=True,
            timeout=60,
        )
        self.assertEqual(2, result.returncode, result.stderr)
        payload = json.loads(result.stdout or result.stderr)
        self.assertEqual("blocked", payload["status"])
        self.assertIn("GARMIN_COACH_LOOP_GATEWAY_STATE_ROOT", payload["error"])
        self.assertIn("GARMIN_COACH_LOOP_TOKEN_HMAC_KEY", payload["error"])


if __name__ == "__main__":
    unittest.main()
