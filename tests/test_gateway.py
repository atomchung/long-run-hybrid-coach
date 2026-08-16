from __future__ import annotations

import base64
import copy
import datetime as dt
import hashlib
import json
import logging
import os
import signal
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any
from unittest import mock

from garmin_coach_loop.gateway import (
    DEPLOYMENT_ENVIRONMENT_ENV_VAR,
    DEPLOYMENT_INSTANCE_ID_ENV_VAR,
    HOSTED_STARTUP_DRAIN_SECONDS,
    INTERVALS_TOKEN_URL,
    RAILWAY_GIT_COMMIT_ENV_VAR,
    RELEASE_ARTIFACT_SHA_ENV_VAR,
    RELEASE_COMMIT_ENV_VAR,
    RELEASE_DOMAIN_ENV_VAR,
    RELEASE_ID_ENV_VAR,
    RELEASE_INSTRUCTIONS_SHA_ENV_VAR,
    RELEASE_OPENAPI_SHA_ENV_VAR,
    CoachGateway,
    CoachGatewayHandler,
    CoachGatewayServer,
    GatewayConfig,
    GatewayConfigError,
    _initialization_claims,
    gateway_artifact_sha256,
    load_config,
    run_gateway,
    run_preflight,
)
from garmin_coach_loop import athlete_evidence
from garmin_coach_loop.delivery import hr_ceiling_percent_lthr
from garmin_coach_loop.release_identity import make_deployment_identity, make_release_id
from garmin_coach_loop.identity import (
    lookup_or_create_owner,
    owner_for_fingerprint,
    record_token_fingerprint,
    scopes_for_fingerprint,
    token_fingerprint,
)
from garmin_coach_loop.plan_init import project_initialization_request
from garmin_coach_loop.proposals import (
    PROPOSAL_TTL_SECONDS,
    binding,
    issue_proposal,
)
from garmin_coach_loop.store import (
    WRITER_CONTRACT_VERSION,
    adopt_store,
    apply_decision,
    canonical_hash,
    default_state_dir,
    doctor_store,
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
DEPLOYMENT_ENVIRONMENT_VALUE = "production"
DEPLOYMENT_INSTANCE_ID_VALUE = "gateway-primary-1"


def load(name: str) -> dict[str, Any]:
    return json.loads((EXAMPLE / name).read_text(encoding="utf-8"))


def publishable_plan() -> dict[str, Any]:
    """The fixture plan with its two running sessions marked deliverable."""
    plan = load("plan-state-v1.json")
    for session in plan["week"]["sessions"]:
        if session["session_id"] in {"run-quality-01", "run-long-01"}:
            session["execution"]["publish_supported"] = True
    return plan


# The account's Run threshold HR, matching the live account `% LTHR` was verified
# against on 2026-08-14. `sport_settings` defaults to a refusal, so a test that delivers
# a heart-rate ceiling opts in by assigning `RUN_SPORT_SETTINGS`.
FIXTURE_RUN_THRESHOLD_HR = 163
RUN_SPORT_SETTINGS = [
    {"types": ["Run"], "threshold_pace": 2.7027, "lthr": FIXTURE_RUN_THRESHOLD_HR}
]


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
        # What Intervals echoes after parsing `50-86% LTHR` out of the workout text: the
        # percentages themselves, against threshold HR. Resolved through the product's own
        # helper so the fake cannot disagree with the text the product actually sent.
        low, high = hr_ceiling_percent_lthr(target["ceiling_bpm"], FIXTURE_RUN_THRESHOLD_HR)
        result["hr"] = {"start": low, "end": high, "units": "%lthr"}
    return result


def _http_error(url: str, status: int) -> urllib.error.HTTPError:
    """A synthetic upstream failure with no response body to read or close."""
    return urllib.error.HTTPError(url, status, "denied", None, None)


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
        self.deleted: list[str] = []
        # Events whose read-back comes back as something other than what was written --
        # the write lands, the verification fails, and the event stays on the calendar.
        self.corrupt_external_ids: set[str] = set()
        self.read_status: int | None = None
        self.token_status: int | None = None
        # Default to a refusal so every optional-settings fallback remains covered. Tests
        # that need a readable settings response assign a list here.
        self.sport_settings: list[dict[str, Any]] | None = None
        self.steps_by_name: dict[str, list[dict[str, Any]]] = {}
        if plan is not None:
            self.register_plan_steps(plan)

    def register_plan_steps(self, plan: dict[str, Any]) -> None:
        """Teach the fake what a workout of this name parses into.

        Intervals derives its own `workout_doc` from the delivered text, so a workout the
        product renames or rewrites is one the provider parses afresh. The fake has no
        parser, so a test that changes a plan registers the result here.
        """
        self.steps_by_name.update({
            session["plan"]["name"]: session["plan"]["steps"]
            for session in plan["week"]["sessions"]
            if session.get("plan", {}).get("kind") == "time_axis"
        })

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
        if method == "GET" and url.endswith("/sport-settings"):
            if self.sport_settings is None:
                raise _http_error(url, 403)
            return json.dumps(self.sport_settings).encode("utf-8")
        if "/activities?" in url:
            return json.dumps(self.activities).encode("utf-8")
        if "/wellness?" in url:
            return json.dumps(self.wellness).encode("utf-8")
        if method == "POST" and "/events/bulk" in url:
            return self._bulk_upsert(json.loads((request.data or b"[]").decode("utf-8")))
        if method == "GET" and "/events?" in url:
            query = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
            oldest = query.get("oldest", [""])[0]
            newest = query.get("newest", [oldest])[0]
            return json.dumps(
                [
                    event
                    for event in self.events
                    if oldest <= str(event.get("start_date_local", ""))[:10] <= newest
                ]
            ).encode("utf-8")
        if method == "DELETE" and "/events/" in url:
            event_id = urllib.parse.urlsplit(url).path.rsplit("/", 1)[-1]
            self.deleted.append(event_id)
            self.events = [
                event for event in self.events if str(event["id"]) != event_id
            ]
            return b""
        if method == "GET" and "/activity/" in url and url.endswith("/intervals"):
            # An activity the provider has not analyzed returns no segments rather than
            # an error, which is the shape the reader is written against.
            return json.dumps({"icu_intervals": []}).encode("utf-8")
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
        if stored.get("external_id") in self.corrupt_external_ids:
            result["workout_doc"] = {"steps": []}
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
        # Movable so a test can let a proposal's lifetime run out without sleeping.
        self.now = NOW
        self.gateway = CoachGateway(self.config, fetch=self.fake, now=lambda: self.now)
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

    def test_exchange_normalizes_and_records_scope_names_only(self):
        self.fake.token_payload = {
            "access_token": TOKEN_A,
            "scope": "WELLNESS:READ, SETTINGS:READ ACTIVITY:READ ignored-value",
            "athlete": {"id": "i1", "name": "provider-name-must-not-persist"},
        }

        status, payload = self._exchange(grant_type="authorization_code", code="c1")

        expected = ("ACTIVITY:READ", "SETTINGS:READ", "WELLNESS:READ")
        self.assertEqual(200, status)
        self.assertEqual(",".join(expected), payload["scope"])
        fingerprint = token_fingerprint(TOKEN_A, hmac_key=HMAC_KEY)
        self.assertEqual(expected, scopes_for_fingerprint(self.identity_db, fingerprint))
        stored = self.identity_db.read_bytes()
        self.assertNotIn(TOKEN_A.encode("utf-8"), stored)
        self.assertNotIn(b"provider-name-must-not-persist", stored)
        self.assertNotIn(b"ignored-value", stored)

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
        # And the earlier token keeps working, because Intervals keeps it valid: an
        # athlete who connects a second client is not signed out of the first.
        self.assertEqual(
            first,
            owner_for_fingerprint(
                self.identity_db, token_fingerprint(TOKEN_A, hmac_key=HMAC_KEY)
            ),
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
        # The provider is read for pre_plan_observations (issue #28), but nothing is
        # written: an account with no plan still has no directory afterwards.
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

    def test_taipei_and_utc_resolve_different_as_of_dates_at_the_same_instant(self):
        # 2026-08-13T18:00:00Z is already 2026-08-14 in Taipei (UTC+8) but still
        # 2026-08-13 in UTC (issue #112): startCoachSession must answer from the
        # athlete's own requested timezone, never from the gateway host's clock or a
        # single hard-coded zone -- the same boundary proven directly against
        # build_window and status_store in tests/test_context_builder.py and
        # tests/test_state_store.py, now proven at the hosted entry point itself.
        self.now = dt.datetime(2026, 8, 13, 18, 0, tzinfo=dt.timezone.utc)

        _, taipei = self.call(
            "POST", "/v1/coach/session", body={"timezone": "Asia/Taipei"}, token=TOKEN_A
        )
        _, utc = self.call("POST", "/v1/coach/session", body={"timezone": "UTC"}, token=TOKEN_A)

        self.assertEqual("2026-08-14", taipei["context"]["as_of"][:10])
        self.assertEqual("2026-08-13", utc["context"]["as_of"][:10])

    def test_omitted_timezone_keeps_the_documented_asia_taipei_default(self):
        self.now = dt.datetime(2026, 8, 13, 18, 0, tzinfo=dt.timezone.utc)

        _, default = self.call("POST", "/v1/coach/session", body={}, token=TOKEN_A)
        _, explicit = self.call(
            "POST", "/v1/coach/session", body={"timezone": "Asia/Taipei"}, token=TOKEN_A
        )

        self.assertEqual(default["context"]["as_of"], explicit["context"]["as_of"])


# --------------------------------------------------------------------------------------
# Bootstrap -- the two paths that turn an authenticated identity into a readable store
# --------------------------------------------------------------------------------------


class GatewayPermissionDiagnosticTests(GatewayTestCase):
    def _exchange_connected_token(self) -> None:
        self.fake.token_payload = {
            "access_token": TOKEN_A,
            "scope": "CALENDAR:WRITE,ACTIVITY:READ,WELLNESS:READ,SETTINGS:READ",
            "athlete": {"id": "i1"},
        }
        status, _ = self.call(
            "POST",
            "/oauth/intervals/token",
            raw=b"grant_type=authorization_code&code=fixture-code",
            content_type="application/x-www-form-urlencoded",
        )
        self.assertEqual(200, status)

    def _assert_redacted_diagnostic_log(self, expected_status: int) -> None:
        logged = "\n".join(self.log_handler.records)
        fingerprint = token_fingerprint(TOKEN_A, hmac_key=HMAC_KEY)
        owner_id = owner_for_fingerprint(self.identity_db, fingerprint)
        self.assertIsNotNone(owner_id)
        for forbidden in (
            TOKEN_A,
            TOKEN_B,
            UNKNOWN_TOKEN,
            fingerprint,
            str(owner_id),
            "i1",
            "provider-settings-must-not-escape",
        ):
            self.assertNotIn(forbidden, logged)
        self.assertIn(
            f"GET /v1/coach/permissions -> {expected_status} access=authenticated", logged
        )

    def test_settings_probe_reports_readable_without_returning_provider_payload(self):
        self._exchange_connected_token()
        self.fake.sport_settings = [{"id": "provider-settings-must-not-escape"}]
        self.log_handler.records.clear()

        status, payload = self.call("GET", "/v1/coach/permissions", token=TOKEN_A)

        self.assertEqual(200, status)
        self.assertEqual("passed", payload["status"])
        self.assertEqual("readable", payload["settings_read"])
        self.assertEqual(
            ["ACTIVITY:READ", "CALENDAR:WRITE", "SETTINGS:READ", "WELLNESS:READ"],
            payload["granted_scopes"],
        )
        rendered = json.dumps(payload)
        for forbidden in (TOKEN_A, "i1", "provider-settings-must-not-escape"):
            self.assertNotIn(forbidden, rendered)
        self.assertEqual("Bearer " + TOKEN_A, self.fake.authorizations[-1])
        self.assertTrue(self.fake.calls[-1][1].endswith("/athlete/0/sport-settings"))
        self._assert_redacted_diagnostic_log(200)

    def test_settings_probe_reports_scope_denied_for_403(self):
        self._exchange_connected_token()
        self.log_handler.records.clear()

        status, payload = self.call("GET", "/v1/coach/permissions", token=TOKEN_A)

        self.assertEqual(200, status)
        self.assertEqual("denied", payload["settings_read"])
        self._assert_redacted_diagnostic_log(200)

    def test_settings_probe_reports_invalid_or_expired_for_401(self):
        self._exchange_connected_token()
        self.fake.read_status = 401
        self.log_handler.records.clear()

        status, payload = self.call("GET", "/v1/coach/permissions", token=TOKEN_A)

        self.assertEqual(200, status)
        self.assertEqual("invalid_or_expired", payload["settings_read"])
        self._assert_redacted_diagnostic_log(200)

    def test_unknown_token_is_refused_before_the_probe(self):
        status, payload = self.call("GET", "/v1/coach/permissions", token=UNKNOWN_TOKEN)

        self.assertEqual(401, status)
        self.assertEqual({"status": "blocked", "error": "unauthorized"}, payload)
        self.assertEqual([], self.fake.calls)

    def test_legacy_identity_db_without_token_scopes_keeps_scope_unknown_and_probes_read_only(self):
        """A production registry predating scope recording must not turn diagnostics into writes.

        The three provider outcomes are intentionally one regression: absence of this
        optional historical observation must never prevent the bounded runtime probe.
        """
        fingerprint = token_fingerprint(TOKEN_A, hmac_key=HMAC_KEY)
        legacy_owner = "legacy-owner-must-not-escape"
        legacy_athlete = "legacy-athlete-must-not-escape"
        with sqlite3.connect(self.identity_db) as connection:
            connection.executescript(
                """
                CREATE TABLE owners (owner_id TEXT PRIMARY KEY, created_at TEXT NOT NULL);
                CREATE TABLE provider_identities (
                    provider TEXT NOT NULL,
                    provider_athlete_id TEXT NOT NULL,
                    owner_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (provider, provider_athlete_id)
                );
                CREATE TABLE token_fingerprints (
                    fingerprint TEXT PRIMARY KEY,
                    owner_id TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                """
            )
            connection.execute(
                "INSERT INTO owners (owner_id, created_at) VALUES (?, ?)",
                (legacy_owner, "2026-08-15T00:00:00Z"),
            )
            connection.execute(
                "INSERT INTO provider_identities (provider, provider_athlete_id, owner_id, created_at)"
                " VALUES (?, ?, ?, ?)",
                ("intervals", legacy_athlete, legacy_owner, "2026-08-15T00:00:00Z"),
            )
            connection.execute(
                "INSERT INTO token_fingerprints (fingerprint, owner_id, provider, created_at)"
                " VALUES (?, ?, ?, ?)",
                (fingerprint, legacy_owner, "intervals", "2026-08-15T00:00:00Z"),
            )
        before = self.identity_db.read_bytes()

        for provider_status, expected in ((200, "readable"), (403, "denied"), (401, "invalid_or_expired")):
            with self.subTest(provider_status=provider_status):
                self.fake.calls.clear()
                self.fake.authorizations.clear()
                self.log_handler.records.clear()
                self.fake.read_status = None if provider_status == 200 else provider_status
                self.fake.sport_settings = (
                    [{"id": "provider-settings-must-not-escape"}]
                    if provider_status == 200
                    else None
                )

                status, payload = self.call("GET", "/v1/coach/permissions", token=TOKEN_A)

                self.assertEqual(200, status)
                self.assertEqual("passed", payload["status"])
                self.assertIsNone(payload["granted_scopes"])
                self.assertEqual(expected, payload["settings_read"])
                self.assertTrue(self.fake.calls[-1][1].endswith("/athlete/0/sport-settings"))
                rendered = json.dumps(payload) + "\n".join(self.log_handler.records)
                for forbidden in (
                    TOKEN_A,
                    fingerprint,
                    legacy_owner,
                    legacy_athlete,
                    "provider-settings-must-not-escape",
                ):
                    self.assertNotIn(forbidden, rendered)
                self.assertIn(
                    "GET /v1/coach/permissions -> 200 access=authenticated", rendered
                )

        self.assertEqual(before, self.identity_db.read_bytes())
        with sqlite3.connect(self.identity_db) as connection:
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
        self.assertNotIn("token_scopes", tables)

    def _assert_invalid_scope_object_fails_closed(self, replacement_ddl: str) -> None:
        self.seed_owner(TOKEN_A)
        fingerprint = token_fingerprint(TOKEN_A, hmac_key=HMAC_KEY)
        with sqlite3.connect(self.identity_db) as connection:
            connection.execute("DROP TABLE token_scopes")
            connection.execute(replacement_ddl)
        self.log_handler.records.clear()

        status, payload = self.call("GET", "/v1/coach/permissions", token=TOKEN_A)

        self.assertEqual(500, status)
        self.assertEqual({"status": "blocked", "error": "internal_error"}, payload)
        self.assertEqual([], self.fake.calls)
        rendered = json.dumps(payload) + "\n".join(self.log_handler.records)
        self.assertNotIn(TOKEN_A, rendered)
        self.assertNotIn(fingerprint, rendered)
        self.assertIn(
            "GET /v1/coach/permissions -> 500 access=authenticated", rendered
        )

    def test_same_name_token_scopes_view_fails_closed(self):
        self._assert_invalid_scope_object_fails_closed(
            "CREATE VIEW token_scopes AS SELECT fingerprint FROM token_fingerprints"
        )

    def test_malformed_token_scopes_table_fails_closed(self):
        self._assert_invalid_scope_object_fails_closed(
            "CREATE TABLE token_scopes (fingerprint TEXT PRIMARY KEY)"
        )


# One real onboarding conversation, in the vocabulary the Action exposes: an athlete who
# runs a bit, lifts at home, and has never had a lab test. Every key below is coaching
# judgment or something they said out loud -- there is no schema version, no plan id, no
# version, no session id, no hash and no delivery flag anywhere in it, and there is no
# PlanState fixture behind it either.
ONBOARDING: dict[str, Any] = {
    "goal": {
        "outcome": "四週後能連續跑完 5 公里不用走路",
        "measurement_protocol": "第 28 天在同一條平路上跑一次 5 公里，記錄總時間與有沒有走路",
    },
    "cycle": {
        "start": "2026-08-17",
        "primary_adaptation": "aerobic_base",
        "maintenance_adaptation": "strength",
        "planned_evidence": ["每週兩次輕鬆跑都完成，而且全程可以講話"],
        "adjust_conditions": ["連續兩週有一次跑步沒做到"],
        "stop_conditions": ["出現疼痛、生病或不尋常症狀時交給人判斷"],
    },
    "week_intent": "先把一週三次的節奏建立起來，這週不安排強度",
    "availability": {
        "days": ["週一晚上", "週三晚上", "週六早上"],
        "equipment": ["可調式啞鈴", "門框單槓"],
    },
    "baselines": {
        "longest_recent_run_km": 3,
        "max_session_minutes": 60,
        "strength_loads": [
            {"exercise": "goblet squat", "load_kg": 16, "scheme": "3x10", "display_name": "高腳杯深蹲"}
        ],
    },
    "sessions": [
        {
            "sport": "running",
            "scheduled_date": "2026-08-19",
            "time_window": "evening",
            "purpose": "建立有氧底子",
            "adaptation": "aerobic_base",
            "body_stress": "lower",
            "cost": "easy",
            "priority": "anchor",
            "planned_minutes": 30,
            "fallback": {"action": "reduce", "description": "縮到 20 分鐘，體感不變"},
            "plan": {
                "kind": "time_axis",
                "name": "30 分鐘輕鬆跑",
                "steps": [
                    {
                        "kind": "work",
                        "name": "輕鬆跑",
                        "duration": {"kind": "time", "seconds": 1800},
                        "target": {"kind": "open"},
                    }
                ],
            },
        },
        {
            "sport": "strength",
            "scheduled_date": "2026-08-17",
            "time_window": "evening",
            "purpose": "維持下肢與上拉力量",
            "adaptation": "strength",
            "body_stress": "full",
            "cost": "moderate",
            "priority": "flexible",
            "planned_minutes": 40,
            "fallback": {"action": "reduce", "description": "深蹲少做一組"},
            "plan": {
                "kind": "movement_list",
                "movements": [
                    {
                        "exercise": "goblet squat", "display_name": "高腳杯深蹲",
                        "sets": 3,
                        "reps": 10,
                        "load_kg": 16,
                        "assist_kg": None,
                        "load_basis": "measured_baseline",
                    },
                    {
                        "exercise": "pull-up", "display_name": "引體向上",
                        "sets": 3,
                        "reps": None,
                        "load_kg": None,
                        "assist_kg": None,
                        "load_basis": "bodyweight",
                    },
                ],
            },
        },
        {
            "sport": "running",
            "scheduled_date": "2026-08-22",
            "time_window": "morning",
            "purpose": "本週第二次有氧曝露",
            "adaptation": "aerobic_base",
            "body_stress": "lower",
            "cost": "easy",
            "priority": "flexible",
            "planned_minutes": 35,
            "fallback": {"action": "reduce", "description": "縮到 25 分鐘"},
            "plan": {
                "kind": "time_axis",
                "name": "35 分鐘輕鬆跑",
                "steps": [
                    {
                        "kind": "work",
                        "name": "輕鬆跑",
                        "duration": {"kind": "time", "seconds": 2100},
                        "target": {"kind": "open"},
                    }
                ],
            },
        },
    ],
    "summary": "先用兩次輕鬆跑加一次全身重訓把週節奏建立起來，配速等到量得出來再談",
    "evidence": [
        {"field": "athlete_reported", "observation": "目前最長跑 3 公里，跑一段要走一段"},
        {"field": "athlete_reported", "observation": "家裡只有啞鈴和單槓，深蹲固定用 16 公斤"},
    ],
    "unknowns": ["還不知道連續三週之後恢復得如何"],
}


def onboarding(**overrides: Any) -> dict[str, Any]:
    request = copy.deepcopy(ONBOARDING)
    request.update(overrides)
    return request


def onboarding_sessions(*sessions: dict[str, Any]) -> dict[str, Any]:
    """The same onboarding conversation with a different first week."""
    return onboarding(sessions=[copy.deepcopy(session) for session in sessions])


def easy_run(**overrides: Any) -> dict[str, Any]:
    session = {
        "sport": "running",
        "scheduled_date": "2026-08-19",
        "purpose": "建立有氧底子",
        "adaptation": "aerobic_base",
        "body_stress": "lower",
        "cost": "easy",
        "priority": "anchor",
        "planned_minutes": 30,
        "fallback": {"action": "reduce", "description": "縮到 20 分鐘"},
        "plan": {
            "kind": "time_axis",
            "name": "30 分鐘輕鬆跑",
            "steps": [
                {
                    "kind": "work",
                    "name": "輕鬆跑",
                    "duration": {"kind": "time", "seconds": 1800},
                    "target": {"kind": "open"},
                }
            ],
        },
    }
    session.update(overrides)
    return session


class GatewayInitializationTests(GatewayTestCase):
    """A first plan authored the way a Custom GPT has to author it.

    Nothing in ``ONBOARDING`` is mechanical, and nothing in it comes from a stored
    artifact. If a caller still had to build a PlanState -- its schema version, its ids,
    its version, its delivery bookkeeping -- none of these tests could be written from an
    ordinary onboarding conversation, which is the whole point of the contract they
    exercise.
    """

    def setUp(self):
        super().setUp()
        # Identity only: this owner has authenticated and owns no state whatsoever.
        self.owner_id = self.seed_owner(TOKEN_A)
        self.state_dir = self.owner_dir(self.owner_id)

    def prepare(
        self, request: dict[str, Any] | None = None, *, token: str | None = TOKEN_A
    ):
        return self.call(
            "POST",
            "/v1/coach/initialization/prepare",
            body={"initialization_request": ONBOARDING if request is None else request},
            token=token,
        )

    def initialize(
        self,
        proposal: str,
        *,
        request: dict[str, Any] | None = None,
        confirmed: Any = True,
        token: str | None = TOKEN_A,
    ):
        body: dict[str, Any] = {
            "initialization_request": ONBOARDING if request is None else request,
            "proposal": proposal,
        }
        if confirmed is not None:
            body["confirmed"] = confirmed
        return self.call(
            "POST", "/v1/coach/initialization/apply", body=body, token=token
        )

    def initialization_proposal(
        self, owner_id: str, request: dict[str, Any], *, now: dt.datetime | None = None
    ) -> str:
        """The proposal prepare would have issued, for cases prepare itself refuses."""
        moment = self.now if now is None else now
        plan = project_initialization_request(request, issued_at=moment)["plan"]
        return issue_proposal(
            _initialization_claims(owner=binding(owner_id, key=HMAC_KEY), initial_plan=plan),
            key=HMAC_KEY,
            now=moment,
        )["proposal"]

    # -- the loop ---------------------------------------------------------------------

    def test_an_empty_account_becomes_a_readable_plan_through_one_confirmation(self):
        status, session = self.call("POST", "/v1/coach/session", body={}, token=TOKEN_A)
        self.assertEqual("no_plan_state", session["status"])
        self.assertFalse(self.state_dir.exists())

        status, prepared = self.prepare()
        self.assertEqual(200, status)
        self.assertEqual("passed", prepared["status"])
        self.assertTrue(prepared["confirmation_required"])

        # The preview carries values, not field names: the athlete's own dates, minutes
        # and wording, plus the two totals that say what the week actually costs.
        preview = prepared["preview"]
        self.assertEqual("四週後能連續跑完 5 公里不用走路", preview["goal"]["outcome"])
        self.assertEqual(["2026-08-17", "2026-08-19", "2026-08-22"],
                         [item["scheduled_date"] for item in preview["sessions"]])
        self.assertEqual("輕鬆跑 30分", preview["sessions"][1]["prescription"])
        self.assertEqual(105, preview["weekly_planned_minutes"])
        self.assertEqual(0, preview["hard_sessions"])
        self.assertFalse(self.state_dir.exists())

        status, applied = self.initialize(prepared["proposal"])
        self.assertEqual(200, status)
        self.assertEqual("passed", applied["status"])
        self.assertEqual(prepared["plan_id"], applied["plan_id"])
        self.assertEqual(1, applied["plan_version"])
        self.assertFalse(applied["idempotent_replay"])

        # What a brand-new conversation sees: the plan it just created, from the store.
        status, session = self.call("POST", "/v1/coach/session", body={}, token=TOKEN_A)
        self.assertEqual(200, status)
        self.assertEqual("passed", session["status"])
        self.assertEqual(applied["plan_id"], session["plan_state"]["plan_id"])
        self.assertEqual(1, session["plan_state"]["plan_version"])
        stored = session["plan_state"]["current_plan"]
        self.assertEqual("四週後能連續跑完 5 公里不用走路", stored["goal"]["outcome"])
        self.assertEqual(
            [item["session_id"] for item in preview["sessions"]],
            [item["session_id"] for item in stored["week"]["sessions"]],
        )

    def test_preparing_an_initialization_writes_nothing_and_reads_no_provider(self):
        status, _ = self.prepare()
        self.assertEqual(200, status)
        self.assertFalse(self.state_dir.exists())
        self.assertFalse((self.state_root / "owners").exists())
        self.assertEqual([], self.fake.calls)

    # -- the profile a new athlete has not stated yet -----------------------------------

    def test_a_new_athlete_is_asked_where_they_are_rather_than_assumed_about(self):
        """A first plan is 28 dated days, and which dates those are depends on where the
        athlete lives. Nobody has said, so the response says nobody has said -- beside
        the plan's other unknowns, in the same words a coach already reads them in.
        """
        _, prepared = self.prepare()

        self.assertIsNone(prepared["athlete_profile"])
        self.assertIn(
            "athlete_profile.timezone is not stated; dates are being read in Asia/Taipei",
            prepared["unknowns"],
        )
        # Not a gate. The athlete who declines to say still gets their plan.
        self.assertEqual("passed", prepared["status"])
        status, applied = self.initialize(prepared["proposal"])
        self.assertEqual(200, status, applied)

    def test_an_athlete_who_already_said_is_not_asked_again(self):
        self.call("POST", "/v1/coach/profile", body={"timezone": "Europe/Berlin"}, token=TOKEN_A)

        _, prepared = self.prepare()

        self.assertEqual("Europe/Berlin", prepared["athlete_profile"]["timezone"])
        self.assertEqual(
            [], [item for item in prepared["unknowns"] if "athlete_profile" in item]
        )

    def test_stating_a_profile_first_does_not_make_the_account_look_used(self):
        # The same guarantee availability has: an athlete may answer "where are you"
        # before there is anything to train, and initialization still runs.
        self.call("POST", "/v1/coach/profile", body={"timezone": "Europe/Berlin"}, token=TOKEN_A)

        _, prepared = self.prepare()
        status, applied = self.initialize(prepared["proposal"])

        self.assertEqual(200, status, applied)
        self.assertEqual(1, applied["plan_version"])

    # -- the days named while setting up the plan (#28) ---------------------------------

    def test_the_days_the_athlete_named_survive_the_conversation_that_named_them(self):
        request = onboarding(
            availability={"days": ["mon", "wed", "sat"], "equipment": ["可調式啞鈴"]}
        )
        _, prepared = self.prepare(request)

        status, applied = self.initialize(prepared["proposal"], request=request)

        self.assertEqual(200, status)
        self.assertNotIn("warnings", applied)
        _, session = self.call("POST", "/v1/coach/session", body={}, token=TOKEN_A)
        constraints = session["context"]["constraints"]
        # The next conversation opens knowing the days rather than asking for them again.
        self.assertEqual(["mon", "wed", "sat"], constraints["available_days"])
        self.assertEqual("athlete_evidence", constraints["availability_source"])

    def test_availability_stated_before_the_plan_does_not_block_creating_one(self):
        # The ordinary first conversation: the athlete answers "which days can you train"
        # in the first message, and the plan is decided several messages later.
        status, _ = self.call(
            "POST",
            "/v1/coach/availability",
            body={"recurring": {"available_days": ["tue", "thu"]}},
            token=TOKEN_A,
        )
        self.assertEqual(200, status)
        request = onboarding(availability={"days": ["mon", "wed", "sat"]})
        _, prepared = self.prepare(request)

        status, applied = self.initialize(prepared["proposal"], request=request)

        self.assertEqual(200, status)
        self.assertEqual(1, applied["plan_version"])
        # The days named while setting the plan up are the later statement, and win.
        _, session = self.call("POST", "/v1/coach/session", body={}, token=TOKEN_A)
        self.assertEqual(
            ["mon", "wed", "sat"], session["context"]["constraints"]["available_days"]
        )

    def test_days_that_cannot_be_read_as_weekdays_warn_and_never_unwind_the_plan(self):
        # The stock onboarding says 週一晚上 / 週三晚上 / 週六早上 -- prose with a time of
        # day in it, which this stores nothing of. The plan is what the athlete confirmed
        # and it stands; the days are simply asked for again.
        _, prepared = self.prepare()

        status, applied = self.initialize(prepared["proposal"])

        self.assertEqual(200, status)
        self.assertEqual("passed", applied["status"])
        self.assertEqual(1, applied["plan_version"])
        self.assertTrue(applied["warnings"])
        self.assertFalse((self.state_dir / "athlete-evidence.json").exists())
        self.assertEqual(1, read_current_plan(self.state_dir)["current_version"])

    # -- what the server owns ----------------------------------------------------------

    def test_every_mechanical_field_is_derived_from_the_request(self):
        _, prepared = self.prepare()
        self.initialize(prepared["proposal"])
        plan = read_current_plan(self.state_dir)["current_plan"]

        self.assertEqual("1.0", plan["schema_version"])
        self.assertEqual(1, plan["version"])
        self.assertEqual("active", plan["status"])
        self.assertEqual(prepared["plan_id"], plan["plan_id"])
        # 28 days inclusive, and the first week starts when the block does.
        self.assertEqual("2026-09-13", plan["cycle"]["end"])
        self.assertEqual("2026-08-17", plan["week"]["start"])

        sessions = {item["session_id"]: item for item in plan["week"]["sessions"]}
        self.assertEqual(
            ["strength-2026-08-17", "running-2026-08-19", "running-2026-08-22"],
            [item["session_id"] for item in plan["week"]["sessions"]],
        )
        for session in sessions.values():
            self.assertEqual("planned", session["match_status"])
            self.assertFalse(session["hard"])  # every session was easy or moderate
            self.assertIsNone(session["execution"]["external_id"])
            self.assertEqual("not_published", session["execution"]["delivery_state"])
        # publish_supported follows what delivery could send: the workout a run's
        # time_axis plan describes, or the purpose that titles a strength calendar entry.
        self.assertTrue(sessions["running-2026-08-19"]["execution"]["publish_supported"])
        self.assertTrue(sessions["running-2026-08-22"]["execution"]["publish_supported"])
        self.assertTrue(sessions["strength-2026-08-17"]["execution"]["publish_supported"])

    def test_hard_follows_the_cost_the_coach_gave(self):
        request = onboarding_sessions(
            easy_run(cost="hard"),
            easy_run(scheduled_date="2026-08-21", priority="flexible"),
        )

        _, prepared = self.prepare(request)
        self.initialize(prepared["proposal"], request=request)

        plan = read_current_plan(self.state_dir)["current_plan"]
        self.assertEqual([True, False], [item["hard"] for item in plan["week"]["sessions"]])
        self.assertEqual(1, prepared["preview"]["hard_sessions"])

    def test_two_sessions_on_one_day_get_distinct_ids(self):
        request = onboarding_sessions(
            easy_run(),
            easy_run(time_window="evening", priority="flexible"),
        )

        _, prepared = self.prepare(request)

        self.assertEqual(
            ["running-2026-08-19", "running-2026-08-19-2"],
            [item["session_id"] for item in prepared["preview"]["sessions"]],
        )

    def test_the_request_cannot_name_a_mechanical_field(self):
        for field, value in (
            ("plan_id", "plan-i-chose"),
            ("version", 1),
            ("schema_version", "1.0"),
            ("status", "active"),
        ):
            status, payload = self.prepare(onboarding(**{field: value}))
            self.assertEqual(400, status, field)
            self.assertEqual("invalid_request", payload["error"], field)
            self.assertIn(field, payload["detail"], field)
        self.assertFalse(self.state_dir.exists())

    def test_a_first_week_with_no_sessions_is_refused_rather_than_filled_in(self):
        status, payload = self.prepare(onboarding(sessions=[]))

        self.assertEqual(400, status)
        self.assertEqual("invalid_request", payload["error"])
        self.assertIn("no default week", payload["detail"])
        self.assertFalse(self.state_dir.exists())

    # -- structure per sport -----------------------------------------------------------

    def test_a_running_session_carries_the_workout_the_watch_executes(self):
        _, prepared = self.prepare()
        self.initialize(prepared["proposal"])

        plan = read_current_plan(self.state_dir)["current_plan"]
        delivered = next(
            item for item in plan["week"]["sessions"] if item["session_id"] == "running-2026-08-19"
        )
        self.assertEqual("time_axis", delivered["plan"]["kind"])
        self.assertEqual("30 分鐘輕鬆跑", delivered["plan"]["name"])
        self.assertEqual(delivered["plan"], prepared["preview"]["sessions"][1]["plan"])
        # And its prescription is the rendering of that plan, not something the request
        # was allowed to write.
        self.assertEqual("輕鬆跑 30分", delivered["prescription"])

    def test_a_strength_session_carries_the_movements_it_prescribes(self):
        _, prepared = self.prepare()
        self.initialize(prepared["proposal"])

        plan = read_current_plan(self.state_dir)["current_plan"]
        lifted = next(
            item for item in plan["week"]["sessions"] if item["sport"] == "strength"
        )
        self.assertEqual("movement_list", lifted["plan"]["kind"])
        self.assertEqual(
            ["goblet squat", "pull-up"],
            [movement["exercise"] for movement in lifted["plan"]["movements"]],
        )
        self.assertEqual(lifted["plan"], prepared["preview"]["sessions"][0]["plan"])
        self.assertEqual("高腳杯深蹲 3x10 16公斤\n引體向上 3組力竭 自重", lifted["prescription"])

    def test_the_superseded_structure_fields_are_no_longer_accepted(self):
        """Both names #93 folded into `plan` are refused as unknown keys.

        This test predates that change and used to prove a sport binding on the request
        shape: `structured_workout` on strength, `strength_movements` on running. That
        binding no longer lives here -- `plan` carries one structure and the validator
        owns which sport may execute which kind -- so what it now proves is narrower and
        worth keeping for its own sake: a payload written against the old schema fails
        loudly instead of being accepted and silently ignored.
        """
        cases = (
            ("structured_workout", {"name": "x", "steps": []}, "strength"),
            (
                "strength_movements",
                [{"exercise": "squat", "display_name": "深蹲", "sets": 3, "reps": 5, "load_kg": None,
                  "assist_kg": None, "load_basis": "bodyweight"}],
                "running",
            ),
        )
        for field, value, sport in cases:
            session = easy_run(sport=sport, **{field: value})
            if sport == "strength":
                session.update(body_stress="full", adaptation="strength", planned_minutes=40,
                               prescription="深蹲 3 組 10 次，自重")
            status, payload = self.prepare(onboarding_sessions(session))
            self.assertEqual(400, status, field)
            self.assertEqual("invalid_request", payload["error"], field)
            self.assertIn(field, payload["detail"], field)

    # -- missing anchors stay unknown --------------------------------------------------

    def test_unmeasured_baselines_stay_null_and_are_named_back(self):
        _, prepared = self.prepare()

        baseline = prepared["preview"]["athlete_baseline"]
        for field in ("threshold_pace_sec_per_km", "max_hr", "easy_hr_ceiling",
                      "weekly_volume_km_4wk_avg"):
            self.assertIsNone(baseline[field], field)
            self.assertIn(f"athlete_baseline.{field} is not measured", prepared["unknowns"])
        # And what the athlete did give is carried through untouched.
        self.assertEqual(3, baseline["longest_recent_run_km"])
        self.assertEqual(60, baseline["max_session_minutes"])
        self.assertEqual(16, baseline["strength_loads"][0]["load_kg"])
        self.assertIsNone(baseline["strength_loads"][0]["assist_kg"])
        self.assertIn("還不知道連續三週之後恢復得如何", prepared["unknowns"])

        self.initialize(prepared["proposal"])
        stored = read_current_plan(self.state_dir)["current_plan"]
        self.assertEqual(baseline, stored["athlete_baseline"])

    PACE_PLAN = {
        "kind": "time_axis",
        "name": "5x1000m",
        "steps": [{
            "kind": "repeat", "repetitions": 5,
            "steps": [{
                "kind": "work", "name": "1000",
                "duration": {"kind": "distance", "meters": 1000},
                "target": {"kind": "pace", "unit": "sec_per_km",
                           "low_seconds_per_km": 360, "high_seconds_per_km": 360},
            }],
        }],
    }

    def test_an_exact_pace_without_a_measured_threshold_is_refused(self):
        status, payload = self.prepare(
            onboarding_sessions(easy_run(plan=copy.deepcopy(self.PACE_PLAN)))
        )

        self.assertEqual(422, status)
        self.assertEqual("validation_failed", payload["error"])
        self.assertIn(
            "threshold_pace_sec_per_km is not measured",
            " ".join(payload["validation"]["errors"]),
        )
        self.assertFalse(self.state_dir.exists())

    def test_the_same_pace_passes_once_the_athlete_supports_it(self):
        """The false-positive control: the anchor makes the identical plan valid."""
        request = onboarding_sessions(easy_run(plan=copy.deepcopy(self.PACE_PLAN)))
        request["baselines"] = {**request["baselines"], "threshold_pace_sec_per_km": 355}

        status, prepared = self.prepare(request)

        self.assertEqual(200, status)
        self.assertEqual("passed", prepared["status"])
        self.assertEqual(355, prepared["preview"]["athlete_baseline"]["threshold_pace_sec_per_km"])

    def test_a_structured_hr_ceiling_without_a_measured_max_is_refused(self):
        status, payload = self.prepare(
            onboarding_sessions(
                easy_run(
                    plan={
                        "kind": "time_axis",
                        "name": "30 分鐘輕鬆跑",
                        "steps": [
                            {
                                "kind": "work",
                                "name": "輕鬆跑",
                                "duration": {"kind": "time", "seconds": 1800},
                                "target": {"kind": "hr_ceiling", "unit": "bpm", "ceiling_bpm": 150},
                            }
                        ],
                    },
                )
            )
        )

        self.assertEqual(422, status)
        self.assertIn(
            "without a measured athlete_baseline.max_hr anchor",
            " ".join(payload["validation"]["errors"]),
        )

    def test_an_exact_kg_load_without_a_matching_baseline_is_refused(self):
        request = onboarding()
        request["baselines"] = {**request["baselines"], "strength_loads": []}

        status, payload = self.prepare(request)

        self.assertEqual(422, status)
        self.assertIn(
            "without a matching established strength baseline",
            " ".join(payload["validation"]["errors"]),
        )

    def test_a_lift_still_to_be_measured_is_expressible_without_a_number(self):
        """The false-positive control: pending_confirmation says the same thing safely."""
        request = onboarding()
        request["baselines"] = {**request["baselines"], "strength_loads": []}
        strength = next(item for item in request["sessions"] if item["sport"] == "strength")
        strength["plan"]["movements"][0].update(
            load_kg=None, load_basis="pending_confirmation"
        )

        status, prepared = self.prepare(request)

        self.assertEqual(200, status)
        self.assertEqual("passed", prepared["status"])
        self.assertIn(
            "athlete_baseline.strength_loads has no measured lift", prepared["unknowns"]
        )

    def test_a_baseline_the_athlete_never_gave_cannot_be_sent_as_zero(self):
        request = onboarding()
        request["baselines"] = {**request["baselines"], "max_hr": 0}

        status, payload = self.prepare(request)

        self.assertEqual(400, status)
        self.assertEqual("invalid_request", payload["error"])
        self.assertIn("null when it is not measured", payload["detail"])

    # -- the binding -------------------------------------------------------------------

    def test_initializing_without_confirmation_creates_nothing(self):
        _, prepared = self.prepare()
        status, payload = self.initialize(prepared["proposal"], confirmed=None)
        self.assertEqual(409, status)
        self.assertEqual("confirmation_required", payload["error"])
        self.assertFalse(self.state_dir.exists())

    def test_a_request_edited_after_the_preview_fails_closed(self):
        _, prepared = self.prepare()
        edited = onboarding()
        edited["sessions"][0]["planned_minutes"] = 75

        status, payload = self.initialize(prepared["proposal"], request=edited)

        self.assertEqual(409, status)
        self.assertEqual("proposal_mismatch", payload["error"])
        self.assertFalse(self.state_dir.exists())

    def test_an_expired_proposal_writes_no_first_plan(self):
        _, prepared = self.prepare()
        self.now = NOW + dt.timedelta(seconds=PROPOSAL_TTL_SECONDS + 1)

        status, payload = self.initialize(prepared["proposal"])

        self.assertEqual(409, status)
        self.assertEqual("proposal_expired", payload["error"])
        self.assertFalse(self.state_dir.exists())

    def test_another_athletes_proposal_confirms_nothing_here(self):
        other_owner = self.seed_owner(TOKEN_B, athlete_id="i2")

        status, payload = self.initialize(
            self.initialization_proposal(other_owner, ONBOARDING)
        )

        self.assertEqual(409, status)
        self.assertEqual("proposal_mismatch", payload["error"])
        self.assertFalse(self.state_dir.exists())

    def test_an_invalid_first_plan_is_refused_by_the_existing_validator(self):
        # A session outside the first week: the projection can build it, the PlanState
        # validator is the one that says no.
        broken = onboarding_sessions(easy_run(scheduled_date="2026-09-02"))

        status, prepared = self.prepare(broken)
        self.assertEqual(422, status)
        self.assertEqual("validation_failed", prepared["error"])
        self.assertIn(
            "must fall in the current week", " ".join(prepared["validation"]["errors"])
        )

        # And the write path refuses it on its own, not only because prepare did.
        status, applied = self.initialize(
            self.initialization_proposal(self.owner_id, broken), request=broken
        )
        self.assertEqual(422, status)
        self.assertEqual("validation_failed", applied["error"])
        self.assertFalse(self.state_dir.exists())

    def test_retrying_the_identical_initialization_replays_the_first_success(self):
        _, prepared = self.prepare()
        self.initialize(prepared["proposal"])
        committed = self.snapshot(self.state_dir)

        status, replayed = self.initialize(prepared["proposal"])

        self.assertEqual(200, status)
        self.assertEqual("passed", replayed["status"])
        self.assertTrue(replayed["idempotent_replay"])
        self.assertEqual(1, replayed["plan_version"])
        self.assertEqual(committed, self.snapshot(self.state_dir))

    def test_a_conflicting_initialization_retry_fails_closed(self):
        _, prepared = self.prepare()
        self.initialize(prepared["proposal"])
        committed = self.snapshot(self.state_dir)

        other = onboarding(week_intent="改成一週四次")
        status, payload = self.initialize(
            self.initialization_proposal(self.owner_id, other), request=other
        )

        self.assertEqual(409, status)
        self.assertEqual("plan_state_exists", payload["error"])
        self.assertEqual(prepared["plan_id"], payload["current_plan_id"])
        self.assertEqual(1, payload["current_plan_version"])
        self.assertEqual(committed, self.snapshot(self.state_dir))

    def test_replaying_an_initialization_onto_a_plan_that_moved_is_refused(self):
        _, prepared = self.prepare()
        self.initialize(prepared["proposal"])
        self.advance_the_plan()
        advanced = self.snapshot(self.state_dir)

        status, payload = self.initialize(prepared["proposal"])

        self.assertEqual(409, status)
        self.assertEqual("plan_state_exists", payload["error"])
        self.assertEqual(2, payload["current_plan_version"])
        self.assertEqual(advanced, self.snapshot(self.state_dir))

    def advance_the_plan(self) -> None:
        """Move the store to v2 through the product's own change route."""
        _, session = self.call("POST", "/v1/coach/session", body={}, token=TOKEN_A)
        change = {
            "summary": "把週三的跑步移到週四",
            "reason_codes": ["schedule_or_equipment_changed"],
            "evidence": [{"field": "athlete_reported", "observation": "週三要加班"}],
            "goal_effect": {"week": "同樣三次，只換一天", "cycle": "28 天方向不變"},
            "next_review_condition": "下週一再看一次",
            "sessions": [
                {
                    "operation": "move",
                    "session_id": "running-2026-08-19",
                    "scheduled_date": "2026-08-20",
                }
            ],
        }
        body = {
            "plan_id": session["plan_state"]["plan_id"],
            "plan_version": session["plan_state"]["plan_version"],
            "context": session["context"],
            "change_request": change,
        }
        status, prepared = self.call("POST", "/v1/coach/decision/prepare", body=body, token=TOKEN_A)
        self.assertEqual(200, status, prepared)
        status, applied = self.call(
            "POST",
            "/v1/coach/decision/apply",
            body={**body, "proposal": prepared["proposal"], "confirmed": True},
            token=TOKEN_A,
        )
        self.assertEqual(200, status, applied)
        self.assertEqual(2, applied["plan_version"])

    def test_preparing_against_an_account_that_already_has_a_plan_is_refused(self):
        _, prepared = self.prepare()
        self.initialize(prepared["proposal"])

        status, payload = self.prepare()

        self.assertEqual(409, status)
        self.assertEqual("plan_state_exists", payload["error"])
        self.assertEqual(1, payload["current_plan_version"])

    def test_an_unknown_token_cannot_initialize_anything(self):
        status, payload = self.prepare(token=UNKNOWN_TOKEN)
        self.assertEqual(401, status)
        self.assertEqual({"status": "blocked", "error": "unauthorized"}, payload)

        status, payload = self.initialize(
            self.initialization_proposal(self.owner_id, ONBOARDING), token=UNKNOWN_TOKEN
        )
        self.assertEqual(401, status)
        self.assertEqual({"status": "blocked", "error": "unauthorized"}, payload)
        self.assertFalse((self.state_root / "owners").exists())
        self.assertEqual([], self.fake.calls)

    def test_a_store_adopted_by_the_operator_is_served_to_that_owner(self):
        """The other bootstrap path, end to end: no request ever creates this store."""
        plan = publishable_plan()
        external = tempfile.TemporaryDirectory()
        self.addCleanup(external.cleanup)
        source = Path(external.name).resolve() / "existing-store"
        init_store(source, plan)

        adopted = adopt_store(source, self.state_dir, confirm=True)
        self.assertEqual("adopted", adopted["status"])

        status, session = self.call("POST", "/v1/coach/session", body={}, token=TOKEN_A)

        self.assertEqual(200, status)
        self.assertEqual("passed", session["status"])
        self.assertEqual("fixture-plan-001", session["plan_state"]["plan_id"])
        self.assertEqual(plan["goal"], session["plan_state"]["current_plan"]["goal"])

    def test_a_second_athlete_initializes_a_disjoint_store(self):
        _, prepared = self.prepare()
        self.initialize(prepared["proposal"])

        other_owner = self.seed_owner(TOKEN_B, athlete_id="i2")
        other = onboarding(week_intent="這位運動員一週只練兩次")
        status, applied = self.call(
            "POST",
            "/v1/coach/initialization/apply",
            body={
                "initialization_request": other,
                "proposal": self.initialization_proposal(other_owner, other),
                "confirmed": True,
            },
            token=TOKEN_B,
        )

        self.assertEqual(200, status)
        self.assertNotEqual(self.owner_id, other_owner)
        self.assertNotEqual(prepared["plan_id"], applied["plan_id"])
        self.assertEqual(
            prepared["plan_id"], read_current_plan(self.state_dir)["plan_id"]
        )
        self.assertEqual(
            applied["plan_id"], read_current_plan(self.owner_dir(other_owner))["plan_id"]
        )

# --------------------------------------------------------------------------------------
# Decisions
# --------------------------------------------------------------------------------------


# One real coaching change, in the vocabulary the Action exposes: Thursday's interval
# session becomes an easy run. Every key below is coaching judgment -- there is no plan
# id, no version, no event id, no hash and no timestamp anywhere in it, and the sessions
# nobody mentioned are never restated.
WEEKLY_CHANGE: dict[str, Any] = {
    "summary": "把週四的間歇換成 45 分鐘輕鬆跑，讓小腿在長跑前恢復",
    "reason_codes": ["multi_signal_recovery_down", "quality_session_conflict"],
    "evidence": [
        {"field": "recovery_trends.hrv", "observation": "HRV 連續三天低於個人基線"},
        {"field": "athlete_reported", "observation": "小腿痠脹，紅旗全部明確為 false"},
    ],
    "goal_effect": {
        "week": "本週少一次高強度刺激，週總時數下降 5 分鐘",
        "cycle": "28 天 threshold 目標不變，下一次 anchor 前重新評估",
    },
    "next_review_condition": "長跑後用實際完成與恢復訊號重新評估",
    "sessions": [
        {
            "operation": "replace",
            "session_id": "run-quality-01",
            "purpose": "以輕鬆有氧取代今天的品質課，保護小腿",
            "adaptation": "aerobic_base",
            "cost": "easy",
            "priority": "flexible",
            "planned_minutes": 45,
            "plan": {
                "kind": "time_axis",
                "name": "45 分鐘輕鬆跑",
                "steps": [
                    {
                        "kind": "work",
                        "name": "輕鬆跑",
                        "duration": {"kind": "time", "seconds": 2700},
                        "target": {"kind": "hr_ceiling", "unit": "bpm", "ceiling_bpm": 150},
                    }
                ],
            },
        }
    ],
}

FROZEN_CHANGE: dict[str, Any] = {
    "summary": "本週維持原樣，週四的品質課照跑",
    "reason_codes": ["plan_kept_no_material_change"],
    "evidence": [{"field": "recovery_trends", "observation": "睡眠與 HRV 都在基線內"}],
    "goal_effect": {"week": "不變", "cycle": "28 天方向不變"},
    "next_review_condition": "長跑後再看一次",
    "sessions": [{"operation": "keep", "session_id": "run-quality-01"}],
}

# Anchored by plan-state-v1's own baseline: the pull-up assistance is measured at 15kg,
# and the bench press load is a number the athlete still owes.
STRENGTH_PLAN: dict[str, Any] = {
    "kind": "movement_list",
    "movements": [
        {
            "exercise": "pull-up",
            "display_name": "引體向上",
            "sets": 3,
            "reps": 8,
            "load_kg": None,
            "assist_kg": 15.0,
            "load_basis": "measured_baseline",
        },
        {
            "exercise": "bench press",
            "display_name": "臥推",
            "sets": 5,
            "reps": 5,
            "load_kg": None,
            "assist_kg": None,
            "load_basis": "pending_confirmation",
        },
    ],
}

STRENGTH_CHANGE: dict[str, Any] = {
    "summary": "上肢課改成輔助引體加臥推，臥推重量等實測再定",
    "reason_codes": ["schedule_or_equipment_changed"],
    "evidence": [{"field": "athlete_reported", "observation": "這週想把上拉排回主課"}],
    "goal_effect": {"week": "上肢刺激不變，動作換掉", "cycle": "28 天方向不變"},
    "next_review_condition": "下一次上肢課後看恢復",
    "sessions": [
        {
            "operation": "replace",
            "session_id": "strength-upper-01",
            "purpose": "上拉為主的上肢課",
            "adaptation": "strength",
            "cost": "moderate",
            "planned_minutes": 45,
            "plan": STRENGTH_PLAN,
        }
    ],
}


def coaching_request(**overrides: Any) -> dict[str, Any]:
    """The coaching half every change request carries, for cases about something else."""
    request: dict[str, Any] = {
        "summary": "調整本週安排",
        "reason_codes": ["schedule_or_equipment_changed"],
        "evidence": [{"field": "constraints", "observation": "本週行程改變"}],
        "goal_effect": {"week": "本週安排調整", "cycle": "28 天方向不變"},
        "next_review_condition": "下一次 anchor 前重新評估",
        "sessions": [],
    }
    request.update(overrides)
    return request


class GatewayDecisionTests(GatewayTestCase):
    """Weekly changes authored the way a Custom GPT has to author them.

    Nothing in these requests is mechanical. If a caller still had to build a PlanState, a
    DecisionEvent, a version, an id or a timestamp, none of these tests could be written
    this way -- which is the whole point of the contract they exercise.
    """

    def setUp(self):
        super().setUp()
        self.before = load("plan-state-v1.json")
        self.context = load("coach-context-day-4.json")
        self.owner_id = self.seed_owner(TOKEN_A, plan=self.before)
        self.state_dir = self.owner_dir(self.owner_id)

    def prepare(
        self,
        change_request: dict[str, Any] | None = None,
        *,
        token: str | None = TOKEN_A,
        **overrides: Any,
    ) -> tuple[int, Any]:
        body = {
            "plan_id": self.before["plan_id"],
            "plan_version": self.before["version"],
            "context": self.context,
            "change_request": WEEKLY_CHANGE if change_request is None else change_request,
        }
        body.update(overrides)
        return self.call("POST", "/v1/coach/decision/prepare", body=body, token=token)

    def apply(
        self,
        proposal: str,
        change_request: dict[str, Any] | None = None,
        *,
        confirmed: Any = True,
        token: str | None = TOKEN_A,
        **overrides: Any,
    ) -> tuple[int, Any]:
        body: dict[str, Any] = {
            "plan_id": self.before["plan_id"],
            "plan_version": self.before["version"],
            "context": self.context,
            "change_request": WEEKLY_CHANGE if change_request is None else change_request,
            "proposal": proposal,
        }
        if confirmed is not None:
            body["confirmed"] = confirmed
        body.update(overrides)
        return self.call("POST", "/v1/coach/decision/apply", body=body, token=token)

    def head_event(self) -> dict[str, Any]:
        commits = sorted(
            path for path in (self.state_dir / "commits").iterdir() if path.is_dir()
        )
        return json.loads((commits[-1] / "event.json").read_text(encoding="utf-8"))

    # -- the two cases the entry has to survive ---------------------------------------

    def test_a_weekly_change_is_authored_from_one_session_response_and_nothing_else(self):
        """The material-change case, start to finish, with no repository fixture in hand.

        Everything the caller sends comes from the previous response or from coaching
        judgment: the plan id and version it was told, the context it was handed back
        verbatim, and one change request naming a session it read.
        """
        status, session = self.call(
            "POST", "/v1/coach/session", body={"all_clear": True}, token=TOKEN_A
        )
        self.assertEqual(200, status, session)
        plan_state = session["plan_state"]
        thursday = next(
            item
            for item in plan_state["current_plan"]["week"]["sessions"]
            if item["scheduled_date"] == "2026-08-13"
        )
        request = copy.deepcopy(WEEKLY_CHANGE)
        request["sessions"][0]["session_id"] = thursday["session_id"]
        # Nothing in it is product structure, at any depth.
        for artifact_field in (
            "plan_id",
            "version",
            "schema_version",
            "event_id",
            "created_at",
            "hard",
            "execution",
            "match_status",
            "proposal",
        ):
            self.assertNotIn(f'"{artifact_field}"', json.dumps(request), artifact_field)
        bundle = {
            "plan_id": plan_state["plan_id"],
            "plan_version": plan_state["plan_version"],
            "context": session["context"],
            "change_request": request,
        }

        status, prepared = self.call(
            "POST", "/v1/coach/decision/prepare", body=bundle, token=TOKEN_A
        )
        self.assertEqual(200, status, prepared)
        self.assertTrue(prepared["confirmation_required"])
        self.assertEqual(2, prepared["resulting_version"])

        status, applied = self.call(
            "POST",
            "/v1/coach/decision/apply",
            body={**bundle, "proposal": prepared["proposal"], "confirmed": True},
            token=TOKEN_A,
        )

        self.assertEqual(200, status, applied)
        self.assertEqual(2, applied["plan_version"])
        self.assertFalse(applied["idempotent_replay"])
        current = read_current_plan(self.state_dir)["current_plan"]
        changed = next(
            item
            for item in current["week"]["sessions"]
            if item["session_id"] == thursday["session_id"]
        )
        self.assertEqual(45, changed["planned_minutes"])
        self.assertEqual("輕鬆跑 45分 心率上限 150 bpm", changed["prescription"])
        self.assertFalse(changed["hard"])
        self.assertEqual("passed", status_store(self.state_dir)["status"])

    def test_a_baseline_update_is_one_hosted_decision_like_any_other(self):
        """AGENTS.md 10 in practice (issue #78): the entry that shows the drift in
        `baseline_evidence` can record the update, without a local CLI in reach.
        The anchor change is part of the confirmed preview, and it lands as an
        ordinary review_week adjust carrying its evidence."""
        request = {
            "summary": "長跑基準更新到 8/14 實際完成的 13.2 公里",
            "reason_codes": ["actual_load_above_plan"],
            "evidence": [
                {
                    "field": "baseline_evidence",
                    "observation": "longest_recent_run_km 12.0 claimed; 13.2 observed on 8/14",
                }
            ],
            "goal_effect": {"week": "本週長跑上限反映實際能力", "cycle": "28 天方向不變"},
            "next_review_condition": "下次長跑後再對照 baseline_evidence",
            "athlete_baseline": {"longest_recent_run_km": 13.2},
        }

        status, prepared = self.prepare(request)
        self.assertEqual(200, status, prepared)
        self.assertTrue(prepared["confirmation_required"])
        block = prepared["preview"]["athlete_baseline"]
        self.assertEqual(12.0, block["before"]["longest_recent_run_km"])
        self.assertEqual(13.2, block["after"]["longest_recent_run_km"])

        status, applied = self.apply(prepared["proposal"], request)
        self.assertEqual(200, status, applied)
        self.assertEqual(2, applied["plan_version"])
        current = read_current_plan(self.state_dir)["current_plan"]
        self.assertEqual(13.2, current["athlete_baseline"]["longest_recent_run_km"])
        event = self.head_event()
        self.assertEqual("review_week", event["mode"])
        self.assertEqual("adjust", event["action"])
        self.assertEqual("passed", status_store(self.state_dir)["status"])

    def test_a_frozen_week_needs_no_confirmation_and_still_records_the_review(self):
        status, prepared = self.prepare(FROZEN_CHANGE)

        self.assertEqual(200, status, prepared)
        self.assertFalse(prepared["confirmation_required"])
        self.assertFalse(prepared["preview"]["material_change"])
        self.assertEqual(1, prepared["resulting_version"])
        self.assertEqual([], prepared["preview"]["sessions"][0]["changed_fields"])

        status, applied = self.apply(
            prepared["proposal"], FROZEN_CHANGE, confirmed=None
        )

        self.assertEqual(200, status, applied)
        self.assertEqual(1, applied["plan_version"])
        self.assertEqual(self.before, read_current_plan(self.state_dir)["current_plan"])
        event = self.head_event()
        self.assertEqual("keep", event["action"])
        self.assertEqual(["plan_kept_no_material_change"], event["reason_codes"])

    # -- what the server owns ----------------------------------------------------------

    def test_unchanged_plan_material_is_copied_by_the_server_not_reauthored(self):
        _, prepared = self.prepare()
        self.apply(prepared["proposal"])

        after = read_current_plan(self.state_dir)["current_plan"]
        stored = {item["session_id"]: item for item in after["week"]["sessions"]}
        untouched = [
            item
            for item in self.before["week"]["sessions"]
            if item["session_id"] != "run-quality-01"
        ]
        self.assertEqual(6, len(untouched))
        for session in untouched:
            self.assertEqual(session, stored[session["session_id"]])
        for field in ("plan_id", "schema_version", "status", "goal", "cycle", "athlete_baseline"):
            self.assertEqual(self.before[field], after[field])
        self.assertEqual(self.before["week"]["intent"], after["week"]["intent"])
        self.assertEqual(self.before["week"]["start"], after["week"]["start"])

    def test_every_mechanical_event_field_is_derived_and_absent_from_the_request(self):
        _, prepared = self.prepare()
        self.apply(prepared["proposal"])

        event = self.head_event()
        for mechanical in (
            "schema_version",
            "event_id",
            "mode",
            "action",
            "plan_id",
            "plan_version_before",
            "plan_version_after",
            "created_at",
            "inputs_used",
        ):
            self.assertNotIn(mechanical, WEEKLY_CHANGE, mechanical)
            self.assertTrue(event[mechanical], mechanical)
        self.assertEqual("review_week", event["mode"])
        self.assertEqual("adjust", event["action"])
        self.assertEqual(1, event["plan_version_before"])
        self.assertEqual(2, event["plan_version_after"])
        self.assertEqual("run-quality-01", event["session_id"])
        self.assertEqual("2026-08-13T00:00:00Z", event["created_at"])
        # The coaching half arrived exactly as written, including the athlete's wording.
        self.assertEqual(WEEKLY_CHANGE["summary"], event["change"]["summary"])
        self.assertEqual(WEEKLY_CHANGE["evidence"], event["evidence"])
        self.assertEqual(WEEKLY_CHANGE["reason_codes"], event["reason_codes"])
        # And what changed is stated in the plan's own values, not reconstructed prose.
        self.assertIn("50min hard", event["change"]["before"])
        self.assertIn("45min easy", event["change"]["after"])

    def test_the_preview_shows_concrete_before_and_after_values(self):
        before_files = self.snapshot(self.state_dir)

        status, prepared = self.prepare()

        self.assertEqual(200, status, prepared)
        preview = prepared["preview"]
        entry = preview["sessions"][0]
        self.assertEqual("replace", entry["operation"])
        self.assertEqual("run-quality-01", entry["session_id"])
        self.assertEqual("2026-08-13", entry["before"]["scheduled_date"])
        self.assertEqual(50, entry["before"]["planned_minutes"])
        self.assertEqual("hard", entry["before"]["cost"])
        self.assertEqual(
            "Warm-up 12分\n5趟：Interval 1公里 配速 6:00/km、Jog recovery 2分\nCool-down 8分",
            entry["before"]["prescription"],
        )
        self.assertEqual(45, entry["after"]["planned_minutes"])
        self.assertEqual("easy", entry["after"]["cost"])
        self.assertEqual("輕鬆跑 45分 心率上限 150 bpm", entry["after"]["prescription"])
        self.assertEqual("45 分鐘輕鬆跑", entry["after"]["workout_name"])
        self.assertIn("planned_minutes", entry["changed_fields"])
        self.assertEqual({"before": 265, "after": 260}, preview["weekly_planned_minutes"])
        self.assertEqual({"before": 1, "after": 0}, preview["hard_sessions"])
        self.assertIsNone(preview["goal"])
        self.assertIsNone(preview["cycle"])
        self.assertEqual(before_files, self.snapshot(self.state_dir))

    def test_a_goal_and_cycle_change_previews_both_sides_and_becomes_a_cycle_review(self):
        request = coaching_request(
            reason_codes=["goal_priority_changed"],
            goal={
                "outcome": "在 28 天內把 10K 跑進 55 分鐘",
                "measurement_protocol": "第 0 天與第 28 天在同一條 10K 路線上重測",
            },
            cycle={"primary_adaptation": "aerobic_base"},
        )

        status, prepared = self.prepare(request)

        self.assertEqual(200, status, prepared)
        preview = prepared["preview"]
        self.assertEqual(
            self.before["goal"]["outcome"], preview["goal"]["before"]["outcome"]
        )
        self.assertEqual(
            "在 28 天內把 10K 跑進 55 分鐘", preview["goal"]["after"]["outcome"]
        )
        self.assertEqual("threshold", preview["cycle"]["before"]["primary_adaptation"])
        self.assertEqual("aerobic_base", preview["cycle"]["after"]["primary_adaptation"])
        # Only the named cycle field moved; the rest was copied.
        self.assertEqual(
            self.before["cycle"]["stop_conditions"],
            preview["cycle"]["after"]["stop_conditions"],
        )

        self.apply(prepared["proposal"], request)
        self.assertEqual("review_cycle", self.head_event()["mode"])

    # -- what one confirmation is bound to ---------------------------------------------

    def test_changing_only_the_context_after_the_preview_fails_to_apply(self):
        _, prepared = self.prepare()
        before_files = self.snapshot(self.state_dir)
        other_context = copy.deepcopy(self.context)
        other_context["unknowns"] = [
            *other_context["unknowns"],
            "evidence the athlete never saw",
        ]

        status, payload = self.apply(prepared["proposal"], context=other_context)

        self.assertEqual(409, status)
        self.assertEqual("proposal_mismatch", payload["error"])
        self.assertEqual(before_files, self.snapshot(self.state_dir))
        self.assertEqual(1, read_current_plan(self.state_dir)["current_version"])

    def test_changing_the_change_request_after_the_preview_fails_to_apply(self):
        _, prepared = self.prepare()
        before_files = self.snapshot(self.state_dir)
        edited = copy.deepcopy(WEEKLY_CHANGE)
        edited["sessions"][0]["planned_minutes"] = 75

        status, payload = self.apply(prepared["proposal"], edited)

        self.assertEqual(409, status)
        self.assertEqual("proposal_mismatch", payload["error"])
        self.assertEqual(before_files, self.snapshot(self.state_dir))

    def test_another_owners_proposal_is_refused_even_when_the_plan_ids_match(self):
        other_owner = self.seed_owner(TOKEN_B, athlete_id="i2", plan=self.before)
        other_dir = self.owner_dir(other_owner)
        _, prepared = self.prepare()
        untouched = self.snapshot(other_dir)

        status, payload = self.apply(prepared["proposal"], token=TOKEN_B)

        self.assertEqual(409, status)
        self.assertEqual("proposal_mismatch", payload["error"])
        self.assertEqual(untouched, self.snapshot(other_dir))
        self.assertEqual("fixture-plan-001", read_current_plan(other_dir)["plan_id"])
        self.assertEqual(1, read_current_plan(other_dir)["current_version"])
        self.assertEqual(1, read_current_plan(self.state_dir)["current_version"])

    def test_an_expired_proposal_writes_nothing(self):
        _, prepared = self.prepare()
        before_files = self.snapshot(self.state_dir)
        self.now = NOW + dt.timedelta(seconds=PROPOSAL_TTL_SECONDS + 1)

        status, payload = self.apply(prepared["proposal"])

        self.assertEqual(409, status)
        self.assertEqual("proposal_expired", payload["error"])
        self.assertEqual(before_files, self.snapshot(self.state_dir))
        self.assertEqual(1, read_current_plan(self.state_dir)["current_version"])

    def test_a_proposal_signed_with_another_key_confirms_nothing(self):
        """The lifetime is only real because the client cannot mint its own proposal."""
        _, prepared = self.prepare()
        encoded = prepared["proposal"].split(".")[0]
        claims = json.loads(
            base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)).decode("utf-8")
        )
        claims.pop("issued_at")
        claims.pop("expires_at")
        forged = issue_proposal(
            claims,
            key=b"a-key-this-gateway-never-had-000",
            now=self.now,
            ttl_seconds=PROPOSAL_TTL_SECONDS * 100,
        )["proposal"]

        status, payload = self.apply(forged)

        self.assertEqual(409, status)
        self.assertEqual("proposal_mismatch", payload["error"])
        self.assertEqual(1, read_current_plan(self.state_dir)["current_version"])

    def test_a_material_change_still_needs_its_one_confirmation(self):
        _, prepared = self.prepare()

        status, payload = self.apply(prepared["proposal"], confirmed=None)

        self.assertEqual(409, status)
        self.assertEqual("confirmation_required", payload["error"])
        self.assertEqual(1, read_current_plan(self.state_dir)["current_version"])

    def test_retrying_the_identical_apply_replays_the_first_success(self):
        _, prepared = self.prepare()
        status, applied = self.apply(prepared["proposal"])
        self.assertEqual(200, status, applied)
        committed = self.snapshot(self.state_dir)

        status, replayed = self.apply(prepared["proposal"])

        self.assertEqual(200, status)
        self.assertTrue(replayed["idempotent_replay"])
        self.assertEqual(2, replayed["plan_version"])
        self.assertEqual(applied["event_id"], replayed["event_id"])
        self.assertEqual(committed, self.snapshot(self.state_dir))
        self.assertEqual(2, len(list((self.state_dir / "commits").iterdir())))

    def test_a_plan_that_moved_after_the_preview_refuses_the_apply(self):
        _, prepared = self.prepare()
        apply_decision(
            self.state_dir,
            context=self.context,
            after=load("plan-state-v2-day-4.json"),
            event=load("decision-event-day-4.json"),
        )
        advanced = self.snapshot(self.state_dir)

        status, payload = self.apply(prepared["proposal"])

        self.assertEqual(409, status)
        self.assertEqual("stale_plan_version", payload["error"])
        self.assertEqual(2, payload["current_plan_version"])
        self.assertEqual(advanced, self.snapshot(self.state_dir))

    def test_prepare_against_another_plan_or_a_stale_version_is_refused(self):
        status, payload = self.prepare(plan_id="somebody-elses-plan")
        self.assertEqual(409, status)
        self.assertEqual("plan_mismatch", payload["error"])

        status, payload = self.prepare(plan_version=99)
        self.assertEqual(409, status)
        self.assertEqual("stale_plan_version", payload["error"])
        self.assertEqual(1, payload["current_plan_version"])

    # -- the validator is still the authority ------------------------------------------

    def test_unsupported_precision_is_still_refused_by_the_unweakened_validator(self):
        request = coaching_request(
            reason_codes=["goal_priority_changed"],
            sessions=[
                {
                    "operation": "replace",
                    "session_id": "strength-upper-01",
                    "purpose": "維持上肢肌力",
                    "adaptation": "strength",
                    "cost": "moderate",
                    "planned_minutes": 50,
                    "plan": {
                        "kind": "movement_list",
                        "movements": [{
                            "exercise": "barbell bench press", "display_name": "槓鈴臥推", "sets": 5, "reps": 5,
                            "load_kg": 80.0, "assist_kg": None,
                            "load_basis": "measured_baseline",
                        }],
                    },
                }
            ],
        )
        before_files = self.snapshot(self.state_dir)

        status, payload = self.prepare(request)

        self.assertEqual(422, status)
        self.assertEqual("validation_failed", payload["error"])
        self.assertTrue(
            any("strength baseline" in error for error in payload["validation"]["errors"]),
            payload["validation"]["errors"],
        )
        self.assertEqual(before_files, self.snapshot(self.state_dir))

    def test_an_explicit_red_flag_blocks_a_weekly_change_that_still_trains_today(self):
        """#84 regression, on the route that produced the hole.

        The athlete says their chest feels tight; the flag lands honestly in the context
        the gateway binds. The change is an ordinary week review -- the only kind this
        route can produce, since mode is derived server-side -- and it even lowers the
        week's load, replacing today's hard session with a 45-minute easy run. It is
        still refused, because the plan it would commit asks a symptomatic athlete to
        train today. Before the boundary read the evidence instead of the mode, this
        returned 200 with a warning and wrote the change on confirmation.
        """
        flagged = copy.deepcopy(self.context)
        flagged["constraints"]["red_flags"]["chest_pain"] = True
        before_files = self.snapshot(self.state_dir)

        status, payload = self.prepare(context=flagged)

        self.assertEqual(422, status, payload)
        self.assertEqual("validation_failed", payload["error"])
        self.assertTrue(
            any(
                "explicit red flag (chest_pain)" in error and "run-quality-01" in error
                for error in payload["validation"]["errors"]
            ),
            payload["validation"]["errors"],
        )
        self.assertEqual(before_files, self.snapshot(self.state_dir))

    def test_an_explicit_red_flag_leaves_a_load_reducing_week_open(self):
        """The false-positive control: a symptom must not freeze the athlete's week.

        Same symptom, same route. This change rests today and trims Sunday's long run,
        which is what responding to a reported symptom actually looks like -- and it goes
        through, previews the lower numbers, and commits on one confirmation.
        """
        flagged = copy.deepcopy(self.context)
        flagged["constraints"]["red_flags"]["chest_pain"] = True
        request = coaching_request(
            summary="胸悶：今天完全休息，週日長跑縮短",
            reason_codes=["pain_or_illness_flag"],
            evidence=[{"field": "athlete_reported", "observation": "今天胸口有點悶"}],
            sessions=[
                {
                    "operation": "replace",
                    "session_id": "run-quality-01",
                    "sport": "rest",
                    "purpose": "回報胸悶，今天不安排訓練",
                    "adaptation": "recovery",
                    "cost": "easy",
                    "planned_minutes": 0,
                    "plan": {"kind": "unstructured"},
                },
                {
                    "operation": "reduce",
                    "session_id": "run-long-01",
                    "planned_minutes": 40,
                    "plan": {
                        "kind": "time_axis",
                        "name": "40 分鐘輕鬆跑",
                        "steps": [
                            {
                                "kind": "work",
                                "name": "輕鬆跑",
                                "duration": {"kind": "time", "seconds": 2400},
                                "target": {
                                    "kind": "hr_ceiling",
                                    "unit": "bpm",
                                    "ceiling_bpm": 150,
                                },
                            }
                        ],
                    },
                },
            ],
        )

        status, prepared = self.prepare(request, context=flagged)

        self.assertEqual(200, status, prepared)
        minutes = prepared["preview"]["weekly_planned_minutes"]
        self.assertLess(minutes["after"], minutes["before"])
        self.assertEqual(0, prepared["preview"]["hard_sessions"]["after"])
        self.assertTrue(
            any("chest_pain" in warning for warning in prepared["warnings"]),
            prepared["warnings"],
        )

        status, applied = self.apply(prepared["proposal"], request, context=flagged)
        self.assertEqual(200, status, applied)
        self.assertEqual(self.before["version"] + 1, applied["plan_version"])

    def test_a_change_request_that_names_no_real_session_is_a_request_error(self):
        request = coaching_request(
            sessions=[
                {
                    "operation": "move",
                    "session_id": "not-in-this-week",
                    "scheduled_date": "2026-08-15",
                }
            ]
        )
        before_files = self.snapshot(self.state_dir)

        status, payload = self.prepare(request)

        self.assertEqual(400, status)
        self.assertEqual("invalid_request", payload["error"])
        self.assertIn("not-in-this-week", payload["detail"])
        self.assertEqual(before_files, self.snapshot(self.state_dir))

    def test_a_mechanical_reason_code_cannot_be_claimed_by_a_coaching_request(self):
        request = coaching_request(reason_codes=["planned_actual_reconciled"])

        status, payload = self.prepare(request)

        self.assertEqual(400, status)
        self.assertEqual("invalid_request", payload["error"])
        self.assertIn("reason_codes", payload["detail"])

    def test_a_strength_change_carries_the_movements_it_now_prescribes(self):
        """Issue #92/#100: the structure a strength session got at birth survives a change.

        End to end, because that is where the claim lives: the movements the request
        carried are the movements the store holds afterwards, and the sentence the athlete
        confirmed in the preview is the rendering of exactly those movements. Issue #100
        had to put the list beside the sentence in the preview to prove they agreed; here
        one generates the other, so the sentence *is* the record.
        """
        status, prepared = self.prepare(STRENGTH_CHANGE)

        self.assertEqual(200, status, prepared)
        self.assertEqual(
            "引體向上 3x8 輔助15公斤\n臥推 5x5 待確認",
            prepared["preview"]["sessions"][0]["after"]["prescription"],
        )
        self.assertEqual(
            "movement_list", prepared["preview"]["sessions"][0]["after"]["plan_kind"]
        )

        status, applied = self.apply(prepared["proposal"], STRENGTH_CHANGE)

        self.assertEqual(200, status, applied)
        current = read_current_plan(self.state_dir)["current_plan"]
        replaced = next(
            item
            for item in current["week"]["sessions"]
            if item["session_id"] == "strength-upper-01"
        )
        self.assertEqual(STRENGTH_PLAN, replaced["plan"])
        self.assertEqual(
            "引體向上 3x8 輔助15公斤\n臥推 5x5 待確認", replaced["prescription"]
        )
        self.assertEqual("passed", status_store(self.state_dir)["status"])

    def test_a_strength_session_may_decline_quantification_and_the_cost_is_named(self):
        """The athlete's decision (2026-08-14): the blank is an answer, priced visibly.

        A strength change declaring `unstructured` goes through end to end -- it is the
        athlete choosing not to enumerate movements, not a defect -- and the response
        carries the warning saying what the blank costs, so the coach can relay it
        instead of the gateway silently accepting less.
        """
        request = coaching_request(
            summary="上肢課改成維持性訓練，今天不指定動作",
            sessions=[
                {
                    "operation": "replace",
                    "session_id": "strength-upper-01",
                    "purpose": "維持刺激就好，動作到場再定",
                    "adaptation": "strength",
                    "cost": "easy",
                    "planned_minutes": 30,
                    "plan": {"kind": "unstructured"},
                }
            ],
        )

        status, prepared = self.prepare(request)

        self.assertEqual(200, status, prepared)
        self.assertEqual(
            "不設定量化目標", prepared["preview"]["sessions"][0]["after"]["prescription"]
        )
        self.assertTrue(
            any(
                "declares no quantified structure" in warning
                for warning in prepared["validation"]["warnings"]
            ),
            prepared["validation"]["warnings"],
        )

        status, applied = self.apply(prepared["proposal"], request)

        self.assertEqual(200, status, applied)
        current = read_current_plan(self.state_dir)["current_plan"]
        replaced = next(
            item
            for item in current["week"]["sessions"]
            if item["session_id"] == "strength-upper-01"
        )
        self.assertEqual({"kind": "unstructured"}, replaced["plan"])
        self.assertEqual("不設定量化目標", replaced["prescription"])
        self.assertEqual("passed", status_store(self.state_dir)["status"])

    def test_lifted_work_on_a_run_never_becomes_a_proposal(self):
        """Issue #100's refusal, at the layer that can still see the whole plan.

        It used to be a request-shape rule -- `strength_movements` rejected unless the
        session's sport was strength -- and so a 400. There is no such field now: a
        movement list is one arm of `plan`, and which arm a sport may declare is a fact
        about the plan being adopted, not about the request naming it. So the refusal is
        the validator's, and the athlete meets it at the same moment: nothing is proposed,
        nothing is stored, and the message says which model the session should have been
        planned under.
        """
        request = coaching_request(
            sessions=[
                {
                    "operation": "reduce",
                    "session_id": "run-long-01",
                    "planned_minutes": 40,
                    "plan": STRENGTH_PLAN,
                }
            ]
        )
        before_files = self.snapshot(self.state_dir)

        status, payload = self.prepare(request)

        self.assertEqual(422, status, payload)
        self.assertEqual("validation_failed", payload["error"])
        self.assertTrue(
            any(
                "run-long-01 must carry a time_axis plan" in error
                for error in payload["validation"]["errors"]
            ),
            payload["validation"],
        )
        self.assertNotIn("proposal", payload)
        self.assertEqual(before_files, self.snapshot(self.state_dir))


# --------------------------------------------------------------------------------------
# Writer-contract guard (issue #88)
# --------------------------------------------------------------------------------------


class GatewayWriterContractTests(GatewayTestCase):
    """The gateway is the other entry path the writer-contract guard has to cover.

    The guard itself lives once in ``garmin_coach_loop.store`` and is exercised directly in
    tests/test_writer_contract.py; this only proves the gateway's own request handling --
    ``apply_decision_request`` calling straight into ``store.apply_decision`` -- actually
    reaches it, through the real HTTP surface a Custom GPT uses.
    """

    def setUp(self):
        super().setUp()
        self.before = load("plan-state-v1.json")
        self.context = load("coach-context-day-4.json")
        self.owner_id = self.seed_owner(TOKEN_A, plan=self.before)
        self.state_dir = self.owner_dir(self.owner_id)

    def prepare(
        self,
        change_request: dict[str, Any] | None = None,
        *,
        token: str | None = TOKEN_A,
        **overrides: Any,
    ) -> tuple[int, Any]:
        body = {
            "plan_id": self.before["plan_id"],
            "plan_version": self.before["version"],
            "context": self.context,
            "change_request": WEEKLY_CHANGE if change_request is None else change_request,
        }
        body.update(overrides)
        return self.call("POST", "/v1/coach/decision/prepare", body=body, token=token)

    def apply(
        self,
        proposal: str,
        change_request: dict[str, Any] | None = None,
        *,
        confirmed: Any = True,
        token: str | None = TOKEN_A,
        **overrides: Any,
    ) -> tuple[int, Any]:
        body: dict[str, Any] = {
            "plan_id": self.before["plan_id"],
            "plan_version": self.before["version"],
            "context": self.context,
            "change_request": WEEKLY_CHANGE if change_request is None else change_request,
            "proposal": proposal,
        }
        if confirmed is not None:
            body["confirmed"] = confirmed
        body.update(overrides)
        return self.call("POST", "/v1/coach/decision/apply", body=body, token=token)

    def bump_store_writer_contract_version(self, delta: int) -> None:
        manifest_path = self.state_dir / "store.json"
        manifest = json.loads(manifest_path.read_text())
        manifest["writer_contract_version"] = WRITER_CONTRACT_VERSION + delta
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    def test_apply_is_refused_before_a_commit_when_the_store_outruns_this_code(self):
        # The preview is issued while the store is still on the code's own contract --
        # only the write attempt that follows sees the store having moved ahead of it.
        _, prepared = self.prepare()
        self.assertTrue(prepared["confirmation_required"])
        commits_before = sorted(p.name for p in (self.state_dir / "commits").iterdir())

        self.bump_store_writer_contract_version(+1)
        status, applied = self.apply(prepared["proposal"])

        self.assertEqual(409, status)
        self.assertEqual("state_conflict", applied["error"])
        self.assertIn(str(WRITER_CONTRACT_VERSION + 1), applied["detail"])
        self.assertIn(str(WRITER_CONTRACT_VERSION), applied["detail"])
        self.assertIn("pull a checkout", applied["detail"])
        commits_after = sorted(p.name for p in (self.state_dir / "commits").iterdir())
        self.assertEqual(commits_before, commits_after)

    def test_a_healthy_store_is_unaffected_by_the_guard(self):
        # A plain sanity control alongside the refusal above: an ordinary, matched-version
        # store still lets a real decision through the same endpoint.
        _, prepared = self.prepare()

        status, applied = self.apply(prepared["proposal"])

        self.assertEqual(200, status, applied)
        self.assertEqual(2, applied["plan_version"])


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

    def test_the_hosted_path_asks_for_the_pace_prerequisite_and_is_refused(self):
        """Issue #131: the export prerequisite is not observable over OAuth, so it is
        not guessed at either.

        The provider can still refuse a settings read, and an unavailable optional
        prerequisite must not be silently guessed. So the hosted entry asks, is told no,
        and delivers exactly as before -- without inferring that the setting is missing,
        and without claiming any hop it still cannot observe.
        """
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
        self.assertEqual(
            2, len([call for call in self.fake.calls if call[1].endswith("/sport-settings")])
        )
        self.assertEqual(2, len(self.fake.bulk_calls))
        self.assertEqual("intervals_accepted", payload["delivery_state"])
        self.assertEqual("intervals_accepted", payload["max_delivery_state"])

    def test_a_readable_and_unset_threshold_pace_blocks_the_hosted_delivery_too(self):
        # The same boundary on both entry points: when the provider does answer, and the
        # answer is that the prerequisite is missing, nothing is written.
        self.fake.sport_settings = [{"types": ["Run"], "threshold_pace": None}]
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

        self.assertEqual(409, status, payload)
        self.assertIn("Run threshold pace", payload["detail"])
        self.assertEqual([], self.fake.bulk_calls)

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
        self.assertEqual("blocked", payload["status"])
        self.assertEqual(
            "missing_or_mismatched_runtime_release_deployment_or_source_identity",
            payload["error"],
        )
        self.assertIsNone(payload["release_identity"])
        self.assertIsNone(payload["deployment_identity"])
        self.assertIsNone(payload["source_git_commit"])
        self.assertEqual([], self.fake.calls)
        self.assertFalse((self.state_root / "owners").exists())

    def test_health_exposes_a_data_free_bound_release_identity(self):
        instructions = "1" * 64
        openapi = "2" * 64
        commit = "a" * 40
        domain = "https://gateway.example"
        identity = {
            "git_commit": commit,
            "instructions_sha256": instructions,
            "openapi_sha256": openapi,
            "gateway_domain": domain,
            "gateway_artifact_sha256": gateway_artifact_sha256(),
        }
        identity["release_id"] = make_release_id(**identity)
        deployment = make_deployment_identity(
            resolved_state_root=self.state_root,
            intervals_client_id=CLIENT_ID_VALUE,
            environment=DEPLOYMENT_ENVIRONMENT_VALUE,
            instance_id=DEPLOYMENT_INSTANCE_ID_VALUE,
            token_hmac_key=HMAC_KEY,
        )
        self.gateway.config = GatewayConfig(
            state_root=self.state_root, token_hmac_key=HMAC_KEY,
            intervals_client_id=CLIENT_ID_VALUE, intervals_client_secret=CLIENT_SECRET_VALUE,
            release_identity=identity,
            deployment_identity=deployment,
            deployed_git_commit=commit,
        )
        status, payload = self.call("GET", "/healthz")
        ready_status, ready_payload = self.call("GET", "/readyz")
        self.assertEqual(200, status)
        self.assertEqual(200, ready_status)
        self.assertEqual(payload, ready_payload)
        self.assertEqual("ok", payload["status"])
        self.assertEqual(identity, payload["release_identity"])
        self.assertEqual(deployment, payload["deployment_identity"])
        self.assertEqual(commit, payload["source_git_commit"])
        serialized = json.dumps(payload, sort_keys=True)
        self.assertNotIn(str(self.state_root), serialized)
        self.assertNotIn(CLIENT_ID_VALUE, serialized)
        self.assertNotIn(HMAC_KEY.decode("ascii"), serialized)

    def test_readiness_refuses_a_source_commit_that_is_not_the_declared_release(self):
        release = {
            "git_commit": "a" * 40,
            "instructions_sha256": "1" * 64,
            "openapi_sha256": "2" * 64,
            "gateway_domain": "https://gateway.example",
            "gateway_artifact_sha256": gateway_artifact_sha256(),
        }
        release["release_id"] = make_release_id(**release)
        self.gateway.config = GatewayConfig(
            state_root=self.state_root,
            token_hmac_key=HMAC_KEY,
            intervals_client_id=CLIENT_ID_VALUE,
            intervals_client_secret=CLIENT_SECRET_VALUE,
            release_identity=release,
            deployment_identity=make_deployment_identity(
                resolved_state_root=self.state_root,
                intervals_client_id=CLIENT_ID_VALUE,
                environment=DEPLOYMENT_ENVIRONMENT_VALUE,
                instance_id=DEPLOYMENT_INSTANCE_ID_VALUE,
                token_hmac_key=HMAC_KEY,
            ),
            deployed_git_commit="b" * 40,
        )

        health_status, health = self.call("GET", "/healthz")
        ready_status, ready = self.call("GET", "/readyz")

        self.assertEqual(200, health_status)
        self.assertEqual(503, ready_status)
        self.assertEqual("blocked", health["status"])
        self.assertEqual(health, ready)
        self.assertEqual("b" * 40, ready["source_git_commit"])

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
        # Requests remain traceable without a stable cross-request owner identifier.
        self.assertIn("POST /v1/coach/session -> 401 access=anonymous", logged)
        self.assertIn("POST /v1/coach/decision/prepare -> 400 access=authenticated", logged)
        self.assertNotIn(owner_id, logged)


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

    def release_env(self) -> dict[str, str]:
        release = {
            "git_commit": "a" * 40,
            "instructions_sha256": "1" * 64,
            "openapi_sha256": "2" * 64,
            "gateway_artifact_sha256": gateway_artifact_sha256(),
            "gateway_domain": "https://gateway.example",
        }
        return {
            RELEASE_ID_ENV_VAR: make_release_id(**release),
            RELEASE_COMMIT_ENV_VAR: release["git_commit"],
            RELEASE_INSTRUCTIONS_SHA_ENV_VAR: release["instructions_sha256"],
            RELEASE_OPENAPI_SHA_ENV_VAR: release["openapi_sha256"],
            RELEASE_DOMAIN_ENV_VAR: release["gateway_domain"],
            RELEASE_ARTIFACT_SHA_ENV_VAR: release["gateway_artifact_sha256"],
        }

    def deployment_env(self) -> dict[str, str]:
        return {
            DEPLOYMENT_ENVIRONMENT_ENV_VAR: DEPLOYMENT_ENVIRONMENT_VALUE,
            DEPLOYMENT_INSTANCE_ID_ENV_VAR: DEPLOYMENT_INSTANCE_ID_VALUE,
        }

    def tearDown(self):
        self._tmp.cleanup()

    def test_complete_configuration_loads(self):
        config = load_config(self.env, host="127.0.0.1", port=9)
        self.assertEqual(self.root, config.state_root)
        self.assertEqual(self.root / "identity.db", config.identity_db_path)
        self.assertEqual(9, config.port)
        self.assertIsNone(config.release_identity)
        self.assertIsNone(config.deployment_identity)
        self.assertEqual(0.0, config.startup_drain_seconds)

    def test_release_configuration_loads_only_with_complete_deployment_identity(self):
        environment = {
            **self.env,
            **self.release_env(),
            **self.deployment_env(),
            RAILWAY_GIT_COMMIT_ENV_VAR: "a" * 40,
        }
        config = load_config(environment)
        self.assertEqual(
            make_deployment_identity(
                resolved_state_root=self.root,
                intervals_client_id=CLIENT_ID_VALUE,
                environment=DEPLOYMENT_ENVIRONMENT_VALUE,
                instance_id=DEPLOYMENT_INSTANCE_ID_VALUE,
                token_hmac_key=HMAC_KEY,
            ),
            config.deployment_identity,
        )
        self.assertEqual("production", config.deployment_identity["environment"])
        self.assertEqual(
            HOSTED_STARTUP_DRAIN_SECONDS,
            config.startup_drain_seconds,
        )
        self.assertEqual("a" * 40, config.deployed_git_commit)

    def test_invalid_railway_source_commit_is_refused_without_echoing_it(self):
        value = "NOT-A-COMMIT"
        with self.assertRaisesRegex(
            GatewayConfigError, RAILWAY_GIT_COMMIT_ENV_VAR
        ) as caught:
            load_config({**self.env, RAILWAY_GIT_COMMIT_ENV_VAR: value})
        self.assertNotIn(value, str(caught.exception))

    def test_release_configuration_blocks_missing_or_partial_deployment_identity(self):
        released = {**self.env, **self.release_env()}
        with self.assertRaisesRegex(GatewayConfigError, "required in release mode"):
            load_config(released)
        for missing in self.deployment_env():
            with self.subTest(missing=missing):
                partial = {
                    **released,
                    **{
                        key: value
                        for key, value in self.deployment_env().items()
                        if key != missing
                    },
                }
                with self.assertRaisesRegex(GatewayConfigError, "incomplete"):
                    load_config(partial)

    def test_partial_or_invalid_deployment_identity_blocks_development_too(self):
        partial = {
            **self.env,
            DEPLOYMENT_ENVIRONMENT_ENV_VAR: DEPLOYMENT_ENVIRONMENT_VALUE,
        }
        with self.assertRaisesRegex(GatewayConfigError, "incomplete"):
            load_config(partial)
        for name, value in (
            (DEPLOYMENT_ENVIRONMENT_ENV_VAR, "Production"),
            (DEPLOYMENT_INSTANCE_ID_ENV_VAR, "gateway primary secret"),
        ):
            with self.subTest(name=name):
                invalid = {**self.env, **self.deployment_env(), name: value}
                with self.assertRaisesRegex(GatewayConfigError, "is invalid") as caught:
                    load_config(invalid)
                self.assertNotIn(value, str(caught.exception))

    def test_deployment_configuration_errors_never_echo_private_values(self):
        released = {**self.env, **self.release_env()}
        with self.assertRaises(GatewayConfigError) as caught:
            load_config(released)
        message = str(caught.exception)
        for private in (
            str(self.root),
            HMAC_KEY.decode("ascii"),
            CLIENT_ID_VALUE,
            CLIENT_SECRET_VALUE,
        ):
            self.assertNotIn(private, message)

    def test_host_and_port_default_to_loopback_when_nothing_names_them(self):
        config = load_config(self.env)
        self.assertEqual("127.0.0.1", config.host)
        self.assertEqual(8422, config.port)

    def test_host_and_port_fall_back_to_the_environment_when_no_flag_is_given(self):
        hosted = dict(
            self.env,
            GARMIN_COACH_LOOP_GATEWAY_HOST="0.0.0.0",
            GARMIN_COACH_LOOP_GATEWAY_PORT="9001",
        )
        config = load_config(hosted)
        self.assertEqual("0.0.0.0", config.host)
        self.assertEqual(9001, config.port)

    def test_an_explicit_flag_wins_over_the_environment(self):
        hosted = dict(
            self.env,
            GARMIN_COACH_LOOP_GATEWAY_HOST="0.0.0.0",
            GARMIN_COACH_LOOP_GATEWAY_PORT="9001",
        )
        config = load_config(hosted, host="10.0.0.5", port=1234)
        self.assertEqual("10.0.0.5", config.host)
        self.assertEqual(1234, config.port)

    def test_a_non_numeric_port_environment_value_is_refused_and_named_without_its_value(self):
        broken = dict(self.env, GARMIN_COACH_LOOP_GATEWAY_PORT="not-a-port")
        with self.assertRaises(GatewayConfigError) as caught:
            load_config(broken)
        message = str(caught.exception)
        self.assertIn("GARMIN_COACH_LOOP_GATEWAY_PORT", message)
        self.assertNotIn("not-a-port", message)

    def test_an_out_of_range_port_environment_value_is_refused(self):
        broken = dict(self.env, GARMIN_COACH_LOOP_GATEWAY_PORT="70000")
        with self.assertRaises(GatewayConfigError):
            load_config(broken)

    def test_no_extra_mcp_origins_are_allowed_unless_the_operator_names_some(self):
        self.assertEqual((), load_config(self.env).allowed_mcp_origins)

    def test_extra_mcp_origins_are_normalized_to_the_triple_that_defines_them(self):
        named = dict(
            self.env,
            GARMIN_COACH_LOOP_MCP_ALLOWED_ORIGINS=(
                "HTTPS://Studio.Example, http://127.0.0.1:5173 ,https://studio.example"
            ),
        )
        self.assertEqual(
            ("https://studio.example", "http://127.0.0.1:5173"),
            load_config(named).allowed_mcp_origins,
        )

    def test_an_entry_that_is_not_an_origin_refuses_startup_rather_than_being_dropped(self):
        # A deployment that looks configured and answers 403 to the client it was
        # configured for is the failure this refusal exists to prevent.
        for value in (
            "https://studio.example/app",
            "studio.example",
            "javascript:alert(1)",
            "https://studio.example, not-an-origin",
            "https://studio.example?x=1",
        ):
            with self.subTest(value=value):
                broken = dict(self.env, GARMIN_COACH_LOOP_MCP_ALLOWED_ORIGINS=value)
                with self.assertRaises(GatewayConfigError) as caught:
                    load_config(broken)
                message = str(caught.exception)
                self.assertIn("GARMIN_COACH_LOOP_MCP_ALLOWED_ORIGINS", message)
                self.assertNotIn(value, message)

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


class GatewayPreflightTests(unittest.TestCase):
    """``run_preflight`` (gateway.py): fail startup on a broken deployment rather than on
    somebody's first request, and reclaim only what a fresh, single-replica process may
    safely treat as abandoned (the deployment contract ``fly.toml`` documents)."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name).resolve() / "state"
        self.config = GatewayConfig(
            state_root=self.root,
            token_hmac_key=HMAC_KEY,
            intervals_client_id=CLIENT_ID_VALUE,
            intervals_client_secret=CLIENT_SECRET_VALUE,
        )

    def tearDown(self):
        self._tmp.cleanup()

    def test_creates_the_state_root_and_identity_registry_before_any_request(self):
        self.assertFalse(self.root.exists())
        reclaimed = run_preflight(self.config)
        self.assertEqual(0, reclaimed)
        self.assertTrue(self.root.is_dir())
        self.assertTrue(self.config.identity_db_path.is_file())

    def test_reclaims_every_owners_stale_lock_and_reports_the_count(self):
        first = self.root / "owners" / "11111111-1111-1111-1111-111111111111"
        second = self.root / "owners" / "22222222-2222-2222-2222-222222222222"
        first.mkdir(parents=True)
        second.mkdir(parents=True)
        (first / ".lock").write_text("pid=1\n")
        (second / ".lock").write_text("pid=2\n")

        reclaimed = run_preflight(self.config)

        self.assertEqual(2, reclaimed)
        self.assertFalse((first / ".lock").exists())
        self.assertFalse((second / ".lock").exists())

    def test_hosted_startup_waits_for_the_predecessor_to_release_a_live_lock(self):
        owner = self.root / "owners" / "66666666-6666-6666-6666-666666666666"
        owner.mkdir(parents=True)
        lock = owner / ".lock"
        lock.write_text("pid=1\n")
        hosted = GatewayConfig(
            state_root=self.config.state_root,
            token_hmac_key=self.config.token_hmac_key,
            intervals_client_id=self.config.intervals_client_id,
            intervals_client_secret=self.config.intervals_client_secret,
            startup_drain_seconds=HOSTED_STARTUP_DRAIN_SECONDS,
        )
        slept: list[float] = []

        def predecessor_finishes(seconds: float) -> None:
            slept.append(seconds)
            self.assertTrue(lock.exists())
            lock.unlink()

        reclaimed = run_preflight(hosted, sleep=predecessor_finishes)

        self.assertEqual([HOSTED_STARTUP_DRAIN_SECONDS], slept)
        self.assertEqual(0, reclaimed)
        self.assertFalse(lock.exists())

    def test_hosted_startup_does_not_wait_when_no_owner_lock_exists(self):
        (self.root / "owners").mkdir(parents=True)
        hosted = GatewayConfig(
            state_root=self.config.state_root,
            token_hmac_key=self.config.token_hmac_key,
            intervals_client_id=self.config.intervals_client_id,
            intervals_client_secret=self.config.intervals_client_secret,
            startup_drain_seconds=HOSTED_STARTUP_DRAIN_SECONDS,
        )

        reclaimed = run_preflight(
            hosted,
            sleep=lambda seconds: self.fail(f"unexpected {seconds}-second wait"),
        )

        self.assertEqual(0, reclaimed)

    def test_never_touches_the_delivery_attempt_journal(self):
        # `.lock` is a process marker; `delivery-attempt.json` is a deliberately durable
        # fence over an in-flight provider write ("A delivery that did not finish fences
        # the store, including recovery"). Reaping the first must never touch the second.
        owner = self.root / "owners" / "33333333-3333-3333-3333-333333333333"
        owner.mkdir(parents=True)
        (owner / ".lock").write_text("pid=1\n")
        (owner / "delivery-attempt.json").write_text('{"kept": true}')

        reclaimed = run_preflight(self.config)

        self.assertEqual(1, reclaimed)
        self.assertFalse((owner / ".lock").exists())
        self.assertEqual('{"kept": true}', (owner / "delivery-attempt.json").read_text())

    def test_an_owner_directory_without_a_lock_is_left_alone(self):
        owner = self.root / "owners" / "44444444-4444-4444-4444-444444444444"
        owner.mkdir(parents=True)
        (owner / "store.json").write_text("{}")

        reclaimed = run_preflight(self.config)

        self.assertEqual(0, reclaimed)
        self.assertTrue((owner / "store.json").exists())

    def test_a_non_directory_entry_under_owners_is_not_mistaken_for_an_owner(self):
        owners_dir = self.root / "owners"
        owners_dir.mkdir(parents=True)
        (owners_dir / "stray-file").write_text("not an owner directory")

        reclaimed = run_preflight(self.config)  # must not raise

        self.assertEqual(0, reclaimed)
        self.assertTrue((owners_dir / "stray-file").exists())

    def test_an_unusable_identity_registry_is_refused(self):
        self.root.mkdir(parents=True)
        self.config.identity_db_path.mkdir()  # a directory where the file should be
        with self.assertRaises(GatewayConfigError):
            run_preflight(self.config)

    @unittest.skipIf(
        hasattr(os, "geteuid") and os.geteuid() == 0, "root bypasses directory permissions"
    )
    def test_a_state_root_that_cannot_be_written_is_refused(self):
        # No permission-restoring cleanup needed: the directory stays empty (the write
        # that would have populated it is exactly what failed), and removing an empty
        # directory needs write access to its parent, not to itself.
        self.root.mkdir(parents=True, mode=0o500)
        with self.assertRaises(GatewayConfigError):
            run_preflight(self.config)


class GatewayShutdownTests(unittest.TestCase):
    """``run_gateway``: SIGTERM and SIGINT both drain in-flight work before exiting.

    A hosting platform's redeploy is a routine SIGTERM; these tests run the real
    ``run_gateway`` entry point over a real loopback socket and real OS signals, the same
    way a platform actually stops the process, rather than exercising a mocked stand-in.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name).resolve()
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.bind(("127.0.0.1", 0))
            self.port = probe.getsockname()[1]
        self.config = GatewayConfig(
            state_root=self.root,
            token_hmac_key=HMAC_KEY,
            intervals_client_id=CLIENT_ID_VALUE,
            intervals_client_secret=CLIENT_SECRET_VALUE,
            host="127.0.0.1",
            port=self.port,
        )

    def tearDown(self):
        self._tmp.cleanup()

    def _wait_until_listening(self, timeout: float = 5.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                with socket.create_connection(("127.0.0.1", self.port), timeout=0.2):
                    return
            except OSError:
                time.sleep(0.02)
        raise AssertionError("gateway never started listening")

    def _assert_signal_causes_a_clean_exit(self, sig: int) -> str:
        """Run the gateway in this thread, send ``sig`` once it is listening, and return
        everything logged during the run so the caller can inspect it."""
        handler = _RecordingHandler()
        logger = logging.getLogger("garmin_coach_loop.gateway")
        previous_level = logger.level
        logger.setLevel(logging.DEBUG)
        logger.addHandler(handler)

        def _send_once_listening() -> None:
            self._wait_until_listening()
            os.kill(os.getpid(), sig)

        sender = threading.Thread(target=_send_once_listening, daemon=True)
        sender.start()
        try:
            run_gateway(self.config, gateway=CoachGateway(self.config))
        finally:
            logger.removeHandler(handler)
            logger.setLevel(previous_level)
        sender.join(timeout=5)

        # The listening socket was actually released, not merely that the call returned.
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as check:
            check.settimeout(0.5)
            with self.assertRaises(OSError):
                check.connect(("127.0.0.1", self.port))
        return "\n".join(handler.records)

    def test_sigterm_causes_a_clean_exit_and_logs_only_the_reclaimed_count(self):
        owner_id = "55555555-5555-5555-5555-555555555555"
        owner = self.root / "owners" / owner_id
        owner.mkdir(parents=True)
        (owner / ".lock").write_text("pid=1\n")

        logged = self._assert_signal_causes_a_clean_exit(signal.SIGTERM)

        self.assertIn("reclaimed 1 stale owner lock(s) at startup", logged)
        self.assertIn("received signal", logged)
        self.assertNotIn(owner_id, logged)
        self.assertNotIn(str(self.root), logged)

    def test_sigint_drains_and_exits_the_same_way_sigterm_does(self):
        logged = self._assert_signal_causes_a_clean_exit(signal.SIGINT)
        self.assertIn("received signal", logged)

    def test_an_in_flight_request_finishes_before_run_gateway_returns(self):
        entered = threading.Event()
        release = threading.Event()

        def slow_fetch(request: urllib.request.Request) -> bytes:
            entered.set()
            release.wait(timeout=10)
            raise _http_error(request.full_url, 403)

        gateway = CoachGateway(self.config, fetch=slow_fetch)
        identity_db = self.config.identity_db_path
        owner_id = lookup_or_create_owner(identity_db, "intervals", "i1")
        record_token_fingerprint(
            identity_db, token_fingerprint(TOKEN_A, hmac_key=HMAC_KEY), owner_id, "intervals"
        )

        request_finished = threading.Event()
        gateway_returned = threading.Event()
        observed: dict[str, Any] = {}

        def do_request() -> None:
            request = urllib.request.Request(
                f"http://127.0.0.1:{self.port}/v1/coach/permissions", method="GET"
            )
            request.add_header("Authorization", "Bearer " + TOKEN_A)
            with urllib.request.urlopen(request, timeout=10) as response:
                observed["status"] = response.status
            request_finished.set()

        # Started here, not inside `drive`, so the test body can join it explicitly
        # below -- joining only `driver` would prove `drive()` itself returned, not that
        # this thread had finished reading the response, and under enough scheduling
        # pressure (the full suite, not this class alone) those two are not the same
        # instant.
        request_thread = threading.Thread(target=do_request, daemon=True)

        def drive() -> None:
            self._wait_until_listening()
            request_thread.start()
            self.assertTrue(entered.wait(timeout=5), "request never reached the slow fetch")
            os.kill(os.getpid(), signal.SIGTERM)
            # `run_gateway` must still be draining -- the request it is waiting on has
            # deliberately not been allowed to finish yet.
            time.sleep(0.5)
            self.assertFalse(gateway_returned.is_set())
            self.assertFalse(request_finished.is_set())
            release.set()

        driver = threading.Thread(target=drive, daemon=True)
        driver.start()
        run_gateway(self.config, gateway=gateway)
        gateway_returned.set()
        driver.join(timeout=5)
        request_thread.join(timeout=5)

        self.assertTrue(request_finished.is_set())
        self.assertEqual(200, observed.get("status"))

    def test_the_cli_process_exits_cleanly_on_sigterm(self):
        """The same property, driven through the real CLI subprocess entry point."""
        environment = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith("GARMIN_COACH_LOOP_")
        }
        environment.update(
            {
                "GARMIN_COACH_LOOP_GATEWAY_STATE_ROOT": str(self.root),
                "GARMIN_COACH_LOOP_TOKEN_HMAC_KEY": HMAC_KEY.decode("ascii"),
                "GARMIN_COACH_LOOP_INTERVALS_CLIENT_ID": CLIENT_ID_VALUE,
                "GARMIN_COACH_LOOP_INTERVALS_CLIENT_SECRET": CLIENT_SECRET_VALUE,
                "GARMIN_COACH_LOOP_GATEWAY_HOST": "127.0.0.1",
                "GARMIN_COACH_LOOP_GATEWAY_PORT": str(self.port),
            }
        )
        process = subprocess.Popen(
            [sys.executable, "-m", "garmin_coach_loop.cli", "serve-gateway"],
            cwd=ROOT,
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            self._wait_until_listening()
            process.send_signal(signal.SIGTERM)
            try:
                out, err = process.communicate(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                out, err = process.communicate()
                self.fail(f"process did not exit after SIGTERM; stderr:\n{err}")
        finally:
            if process.poll() is None:
                process.kill()
                process.communicate()

        self.assertEqual(0, process.returncode, err)
        report = json.loads(out)
        self.assertEqual("passed", report["status"])


class GatewayWithdrawalTests(GatewayDeliveryTests):
    """Issue #113: the hosted athlete can also remove a workout their change superseded."""

    def _publish_one(self) -> str:
        prepared = self.prepare_set(["run-quality-01"])
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
        return payload["delivered"][0]["external_id"]

    def _supersede(self) -> None:
        from garmin_coach_loop.plan_change import project_change_request
        from garmin_coach_loop.validation import (
            _expected_context_baseline,
            _expected_current_calendar,
            _expected_goal_context,
        )

        before = read_current_plan(self.state_dir)["current_plan"]
        context = load("coach-context-day-4.json")
        context["goal_context"] = _expected_goal_context(before)
        context["athlete_baseline"] = _expected_context_baseline(before)
        context["current_calendar"] = _expected_current_calendar(before)
        projection = project_change_request(
            before,
            {
                "summary": "改成完全休息",
                "reason_codes": ["schedule_or_equipment_changed"],
                "evidence": [{"field": "constraints", "observation": "本週行程改變"}],
                "goal_effect": {"week": "本週安排調整", "cycle": "28 天方向不變"},
                "next_review_condition": "下一次 anchor 前重新評估",
                "sessions": [
                    {
                        "operation": "replace",
                        "session_id": "run-quality-01",
                        "sport": "rest",
                        "purpose": "完全休息",
                        "adaptation": "recovery",
                        "cost": "easy",
                        "planned_minutes": 0,
                        "plan": {"kind": "unstructured"},
                    }
                ],
            },
            context=context,
            issued_at=dt.datetime(2026, 8, 13, 0, 0, tzinfo=dt.timezone.utc),
        )
        apply_decision(
            self.state_dir,
            context=context,
            after=projection["after_plan"],
            event=projection["decision_event"],
        )

    def test_one_confirmation_removes_the_event_the_change_superseded(self):
        delivered_id = self._publish_one()
        self._supersede()
        current = read_current_plan(self.state_dir)

        status, prepared = self.call(
            "POST",
            "/v1/coach/delivery/withdraw/prepare",
            body={
                "plan_id": current["plan_id"],
                "plan_version": current["current_version"],
                "session_ids": ["run-quality-01"],
            },
            token=TOKEN_A,
        )
        self.assertEqual(200, status, prepared)
        self.assertTrue(prepared["confirmation_required"])
        self.assertEqual(
            [delivered_id],
            [item["superseded_external_id"] for item in prepared["preview"]],
        )
        self.assertEqual([], self.fake.deleted)

        status, payload = self.call(
            "POST",
            "/v1/coach/delivery/withdraw/apply",
            body={
                "withdrawal_set": prepared["withdrawal_set"],
                "proposal_hash": prepared["proposal_hash"],
                "confirmed": True,
            },
            token=TOKEN_A,
        )

        self.assertEqual(200, status, payload)
        self.assertEqual("passed", payload["status"])
        self.assertEqual([delivered_id], self.fake.deleted)
        self.assertEqual([], payload["unresolved"])
        self.assertEqual([], self.fake.events)
        session = next(
            item
            for item in read_current_plan(self.state_dir)["current_plan"]["week"]["sessions"]
            if item["session_id"] == "run-quality-01"
        )
        self.assertNotIn("superseded_external_id", session["execution"])

    def test_the_athlete_s_own_timezone_decides_which_days_are_already_past(self):
        # Same defect #112 fixed for status and startCoachSession: the day that decides
        # what may be removed has to be the athlete's, not the server's default.
        self._publish_one()
        self._supersede()
        current = read_current_plan(self.state_dir)
        _, prepared = self.call(
            "POST",
            "/v1/coach/delivery/withdraw/prepare",
            body={
                "plan_id": current["plan_id"],
                "plan_version": current["current_version"],
                "session_ids": ["run-quality-01"],
            },
            token=TOKEN_A,
        )
        body = {
            "withdrawal_set": prepared["withdrawal_set"],
            "proposal_hash": prepared["proposal_hash"],
            "confirmed": True,
        }

        status, payload = self.call(
            "POST",
            "/v1/coach/delivery/withdraw/apply",
            body={**body, "timezone": "America/New_York"},
            token=TOKEN_A,
        )

        # NOW is 2026-08-13T00:00Z, which is still 2026-08-12 in New York, so the
        # delivered day has not passed there and the withdrawal goes through.
        self.assertEqual(200, status, payload)
        self.assertEqual("passed", payload["status"])

    def test_a_stored_timezone_decides_the_same_day_a_withdrawal_answers_from(self):
        """The request says nothing about where the athlete is, because they already did.

        The event is dated 2026-08-13. At 18:00Z that day it is already the 14th in the
        deployment's own default zone, so the day has passed and the withdrawal is
        refused -- while for an athlete who told us they live in UTC it is still the 13th
        and the same call goes through. The request bodies are identical; only the stored
        profile differs.
        """
        self._publish_one()
        self._supersede()
        self.now = dt.datetime(2026, 8, 13, 18, 0, tzinfo=dt.timezone.utc)
        current = read_current_plan(self.state_dir)
        _, prepared = self.call(
            "POST",
            "/v1/coach/delivery/withdraw/prepare",
            body={
                "plan_id": current["plan_id"],
                "plan_version": current["current_version"],
                "session_ids": ["run-quality-01"],
            },
            token=TOKEN_A,
        )
        body = {
            "withdrawal_set": prepared["withdrawal_set"],
            "proposal_hash": prepared["proposal_hash"],
            "confirmed": True,
        }

        status, payload = self.call(
            "POST", "/v1/coach/delivery/withdraw/apply", body=body, token=TOKEN_A
        )
        self.assertEqual(409, status, payload)
        self.assertEqual([], self.fake.deleted)

        athlete_evidence.record_profile(self.state_dir, timezone="UTC", now=self.now)
        status, payload = self.call(
            "POST", "/v1/coach/delivery/withdraw/apply", body=body, token=TOKEN_A
        )
        self.assertEqual(200, status, payload)
        self.assertEqual(["9001"], self.fake.deleted)

    def test_an_unresolvable_timezone_on_a_withdrawal_is_one_actionable_error(self):
        self._publish_one()
        self._supersede()
        current = read_current_plan(self.state_dir)
        _, prepared = self.call(
            "POST",
            "/v1/coach/delivery/withdraw/prepare",
            body={
                "plan_id": current["plan_id"],
                "plan_version": current["current_version"],
                "session_ids": ["run-quality-01"],
            },
            token=TOKEN_A,
        )

        status, payload = self.call(
            "POST",
            "/v1/coach/delivery/withdraw/apply",
            body={
                "withdrawal_set": prepared["withdrawal_set"],
                "proposal_hash": prepared["proposal_hash"],
                "confirmed": True,
                "timezone": "Mars/Olympus_Mons",
            },
            token=TOKEN_A,
        )

        self.assertEqual(400, status, payload)
        self.assertIn("Mars/Olympus_Mons", payload["detail"])
        self.assertEqual([], self.fake.deleted)

    def test_an_unconfirmed_withdrawal_deletes_nothing(self):
        self._publish_one()
        self._supersede()
        current = read_current_plan(self.state_dir)
        _, prepared = self.call(
            "POST",
            "/v1/coach/delivery/withdraw/prepare",
            body={
                "plan_id": current["plan_id"],
                "plan_version": current["current_version"],
                "session_ids": ["run-quality-01"],
            },
            token=TOKEN_A,
        )

        status, payload = self.call(
            "POST",
            "/v1/coach/delivery/withdraw/apply",
            body={
                "withdrawal_set": prepared["withdrawal_set"],
                "proposal_hash": prepared["proposal_hash"],
            },
            token=TOKEN_A,
        )

        self.assertEqual(409, status)
        self.assertEqual("confirmation_required", payload["error"])
        self.assertEqual([], self.fake.deleted)

    def test_the_divergence_is_visible_before_anyone_asks_to_withdraw(self):
        delivered_id = self._publish_one()
        self._supersede()
        status, payload = self.call("POST", "/v1/coach/session", body={}, token=TOKEN_A)
        self.assertEqual(200, status, payload)
        session = next(
            item
            for item in payload["delivery"]["sessions"]
            if item["session_id"] == "run-quality-01"
        )
        self.assertEqual("not_published", session["delivery_state"])
        self.assertEqual(delivered_id, session["superseded_external_id"])


class AthleteProfileRouteTests(GatewayTestCase):
    """Where the athlete is and what they read, stated once and never asked for again.

    Both were deployment constants, which is invisible to an owner who happens to live in
    the deployment's own timezone and read its language, and wrong in every conversation
    for anybody else. So what these tests check is continuity across a boundary: a
    statement made through one entry answering a question asked through another, and the
    day every route agrees on being the athlete's own.
    """

    def setUp(self):
        super().setUp()
        self.owner_id = self.seed_owner(TOKEN_A, plan=publishable_plan())
        self.state_dir = self.owner_dir(self.owner_id)

    def profile(self, body: dict[str, Any], *, token: str | None = TOKEN_A):
        return self.call("POST", "/v1/coach/profile", body=body, token=token)

    def session(self, body: dict[str, Any] | None = None):
        return self.call("POST", "/v1/coach/session", body=body or {}, token=TOKEN_A)

    # -- the route ---------------------------------------------------------------------

    def test_a_profile_is_stored_and_echoed_back_with_what_is_now_in_force(self):
        status, payload = self.profile({"timezone": "Europe/Berlin", "language": "en"})

        self.assertEqual(200, status, payload)
        self.assertEqual("passed", payload["status"])
        self.assertEqual("Europe/Berlin", payload["profile"]["timezone"])
        self.assertEqual("en", payload["profile"]["language"])
        self.assertEqual(
            {"timezone": "Europe/Berlin", "language": "en"}, payload["effective"]
        )

    def test_one_field_at_a_time_leaves_the_other_where_it_was(self):
        self.profile({"timezone": "Europe/Berlin"})

        _, payload = self.profile({"language": "en"})

        self.assertEqual("Europe/Berlin", payload["profile"]["timezone"])
        self.assertEqual("en", payload["profile"]["language"])

    def test_a_language_nothing_can_render_is_refused_without_storing_anything(self):
        status, payload = self.profile({"language": "fr"})

        self.assertEqual(400, status, payload)
        self.assertEqual("invalid_request", payload["error"])
        self.assertFalse((self.state_dir / "athlete-evidence.json").exists())

    def test_a_field_the_route_was_never_taught_is_named_rather_than_ignored(self):
        status, payload = self.profile({"timezone": "Europe/Berlin", "locale": "de-DE"})

        self.assertEqual(400, status, payload)
        self.assertIn("locale", payload["detail"])

    def test_another_athletes_profile_is_never_reachable(self):
        self.seed_owner(TOKEN_B, athlete_id="i2", plan=publishable_plan())
        self.profile({"timezone": "Europe/Berlin"})

        _, other = self.call("POST", "/v1/coach/session", body={}, token=TOKEN_B)

        self.assertIsNone(other["context"]["athlete_profile"])

    # -- said once, and every later call already knows ---------------------------------

    def test_the_next_conversation_does_not_have_to_state_the_timezone_again(self):
        # 2026-08-13T18:00Z is already the 14th in Taipei and still the 13th in UTC.
        self.now = dt.datetime(2026, 8, 13, 18, 0, tzinfo=dt.timezone.utc)
        self.profile({"timezone": "UTC"})

        # A brand-new conversation: an empty body, exactly as an agent that was never
        # told where the athlete lives would send.
        _, payload = self.session()

        self.assertEqual("2026-08-13", payload["context"]["as_of"][:10])
        self.assertEqual("UTC", payload["context"]["timezone"])

    def test_a_timezone_stated_at_the_cli_is_the_one_the_hosted_entry_reads(self):
        # The two entries are two front doors onto one athlete's state, so a fact stated
        # at either has to be in force at the other.
        self.now = dt.datetime(2026, 8, 13, 18, 0, tzinfo=dt.timezone.utc)
        athlete_evidence.record_profile(self.state_dir, timezone="UTC", now=self.now)

        _, payload = self.session()

        self.assertEqual("2026-08-13", payload["context"]["as_of"][:10])

    def test_a_request_timezone_is_a_one_off_override_of_the_stored_one(self):
        self.now = dt.datetime(2026, 8, 13, 18, 0, tzinfo=dt.timezone.utc)
        self.profile({"timezone": "UTC"})

        _, overridden = self.session({"timezone": "Asia/Taipei"})
        _, stored = self.session()

        self.assertEqual("2026-08-14", overridden["context"]["as_of"][:10])
        # And the override did not become the athlete's new home.
        self.assertEqual("2026-08-13", stored["context"]["as_of"][:10])
        self.assertEqual("UTC", stored["context"]["athlete_profile"]["timezone"])

    def test_an_override_that_is_not_an_iana_zone_is_still_refused_outright(self):
        self.profile({"timezone": "UTC"})

        status, payload = self.session({"timezone": "Nowhere/Nothing"})

        self.assertEqual(400, status, payload)
        self.assertEqual("invalid_request", payload["error"])

    def test_the_context_says_whether_anybody_ever_stated_a_profile(self):
        _, before = self.session()
        self.assertIsNone(before["context"]["athlete_profile"])

        self.profile({"timezone": "Europe/Berlin", "language": "en"})
        _, after = self.session()

        self.assertEqual("Europe/Berlin", after["context"]["athlete_profile"]["timezone"])
        self.assertEqual("en", after["context"]["athlete_profile"]["language"])

    def test_an_unmigrated_store_answers_exactly_as_it_did_before(self):
        """The owner's own store, which predates the profile entirely.

        Nothing about it changes: the same day, the same language, and no evidence file
        brought into being by a read.
        """
        self.now = dt.datetime(2026, 8, 13, 18, 0, tzinfo=dt.timezone.utc)

        _, silent = self.session()
        _, explicit = self.session({"timezone": "Asia/Taipei"})

        self.assertEqual(explicit["context"]["as_of"], silent["context"]["as_of"])
        self.assertEqual("Asia/Taipei", silent["context"]["timezone"])
        self.assertFalse((self.state_dir / "athlete-evidence.json").exists())


class NonChineseAthleteJourneyTests(GatewayTestCase):
    """The same plan, written for an athlete who does not read Chinese.

    The structure is untouched -- same sessions, same numbers, same movements. Only the
    sentence wrapped around them changes, and it has to survive the whole way: through
    the store's own validation of every commit, out to Intervals as an event description
    and a calendar title, and back through the exact read-back that verifies a delivery.
    """

    def setUp(self):
        super().setUp()
        self.owner_id = self.seed_owner(TOKEN_A)
        self.state_dir = self.owner_dir(self.owner_id)

    def _initialize(self) -> dict[str, Any]:
        status, prepared = self.call(
            "POST",
            "/v1/coach/initialization/prepare",
            body={"initialization_request": ONBOARDING},
            token=TOKEN_A,
        )
        self.assertEqual(200, status, prepared)
        status, applied = self.call(
            "POST",
            "/v1/coach/initialization/apply",
            body={
                "initialization_request": ONBOARDING,
                "proposal": prepared["proposal"],
                "confirmed": True,
            },
            token=TOKEN_A,
        )
        self.assertEqual(200, status, applied)
        return prepared

    def _sessions(self) -> dict[str, dict[str, Any]]:
        plan = read_current_plan(self.state_dir)["current_plan"]
        return {session["session_id"]: session for session in plan["week"]["sessions"]}

    def test_the_english_plan_is_the_chinese_plan_with_a_different_sentence(self):
        self.call("POST", "/v1/coach/profile", body={"language": "en"}, token=TOKEN_A)

        prepared = self._initialize()

        sessions = self._sessions()
        run = next(item for item in sessions.values() if item["sport"] == "running")
        strength = next(item for item in sessions.values() if item["sport"] == "strength")
        self.assertEqual("輕鬆跑 30 min", run["prescription"])
        # The movements keep the athlete's own words -- display_name was always theirs,
        # and translating it would rename the lift they asked for.
        self.assertEqual(
            "高腳杯深蹲 3x10 16 kg\n引體向上 3 sets to failure bodyweight",
            strength["prescription"],
        )
        # Structure untouched: the preview the athlete confirmed is the plan that landed.
        self.assertEqual(
            [item["scheduled_date"] for item in prepared["preview"]["sessions"]],
            [item["scheduled_date"] for item in sorted(
                sessions.values(), key=lambda item: item["scheduled_date"]
            )],
        )
        self.assertEqual(16, strength["plan"]["movements"][0]["load_kg"])
        # And the store validates every commit it holds, including this prose.
        self.assertEqual("passed", doctor_store(self.state_dir)["status"])

    def test_the_english_sentence_reaches_intervals_and_passes_read_back(self):
        self.call("POST", "/v1/coach/profile", body={"language": "en"}, token=TOKEN_A)
        self._initialize()
        current = read_current_plan(self.state_dir)
        strength_id = next(
            session_id
            for session_id, session in self._sessions().items()
            if session["sport"] == "strength"
        )

        status, prepared = self.call(
            "POST",
            "/v1/coach/delivery/prepare",
            body={
                "plan_id": current["plan_id"],
                "plan_version": current["current_version"],
                "session_ids": [strength_id],
            },
            token=TOKEN_A,
        )
        self.assertEqual(200, status, prepared)
        status, published = self.call(
            "POST",
            "/v1/coach/delivery/publish",
            body={
                "delivery_set": prepared["delivery_set"],
                "proposal_hash": prepared["proposal_hash"],
                "confirmed": True,
            },
            token=TOKEN_A,
        )

        # Read back from the provider and verified byte for byte -- the same gate every
        # delivery passes, unchanged by the language it carries.
        self.assertEqual(200, status, published)
        self.assertEqual("intervals_accepted", published["delivery_state"])
        written = self.fake.bulk_calls[0]
        # The calendar title an athlete actually sees on the watch, and the description
        # under it -- one language, because the title is written in the sentence's own.
        self.assertEqual("維持下肢與上拉力量: 高腳杯深蹲 3x10 16kg", written["name"])
        self.assertEqual(
            "高腳杯深蹲 3x10 16 kg\n引體向上 3 sets to failure bodyweight",
            written["description"],
        )

    def test_the_same_plan_without_a_language_is_written_exactly_as_it_always_was(self):
        self._initialize()

        strength = next(
            item for item in self._sessions().values() if item["sport"] == "strength"
        )
        self.assertEqual(
            "高腳杯深蹲 3x10 16公斤\n引體向上 3組力竭 自重", strength["prescription"]
        )


class AthleteEvidenceRouteTests(GatewayTestCase):
    """Storing what the athlete said, then reading it back in a later conversation.

    The two routes exist because a hosted athlete's statements had nowhere to live: the
    only durable memory is the PlanState, and neither "I can't train Wednesday" nor
    "bench 65 by 4" belongs in one. What these tests actually check is continuity --
    a statement made in one conversation answering a question asked in the next -- because
    that is the whole feature; a route that stored perfectly and was never read back would
    pass a narrower test and deliver nothing (#28, #47).
    """

    def setUp(self):
        super().setUp()
        self.owner_id = self.seed_owner(TOKEN_A, plan=publishable_plan())
        self.state_dir = self.owner_dir(self.owner_id)

    def availability(self, body: dict[str, Any], *, token: str | None = TOKEN_A):
        return self.call("POST", "/v1/coach/availability", body=body, token=token)

    def strength(self, body: dict[str, Any], *, token: str | None = TOKEN_A):
        return self.call("POST", "/v1/coach/strength-report", body=body, token=token)

    def session(self, *, token: str | None = TOKEN_A, body: dict[str, Any] | None = None):
        return self.call("POST", "/v1/coach/session", body=body or {}, token=token)

    # -- the two routes ----------------------------------------------------------------

    def test_availability_is_stored_and_echoed_back_as_what_now_holds(self):
        status, payload = self.availability(
            {"recurring": {"available_days": ["mon", "wed", "fri"], "unavailable_days": ["sun"]}}
        )

        self.assertEqual(200, status)
        self.assertEqual("passed", payload["status"])
        self.assertEqual(["mon", "wed", "fri"], payload["recurring"]["available_days"])
        self.assertEqual(["sun"], payload["recurring"]["unavailable_days"])
        self.assertIsNone(payload["week"])
        self.assertEqual("recurring", payload["effective_this_week"]["basis"])
        self.assertEqual("2026-08-10", payload["effective_this_week"]["week_start"])

    def test_a_strength_report_is_stored_once_however_many_times_it_arrives(self):
        body = {
            "date": "2026-08-12",
            "exercise": "bench press",
            "category": "chest",
            "sets": [{"set": 1, "weight_kg": 65, "reps": 4}],
        }

        first_status, first = self.strength(body)
        second_status, second = self.strength(body)

        self.assertEqual((200, 200), (first_status, second_status))
        self.assertFalse(first["idempotent_replay"])
        self.assertTrue(second["idempotent_replay"])
        self.assertEqual(first["report_id"], second["report_id"])
        self.assertEqual(1, second["report_count"])

    def prescribed(self, body: dict[str, Any], *, token: str | None = TOKEN_A):
        return self.call("POST", "/v1/coach/strength-prescribed", body=body, token=token)

    def test_confirming_a_planned_session_needs_only_its_id(self):
        """Issue #76: the plan holds the sets, so the athlete does not read them back.

        This is the whole point of the route -- one sentence, no dictation -- and the
        check that matters is that the next conversation sees per-set execution the
        athlete never enumerated.
        """
        status, payload = self.prescribed({"session_id": "strength-full-01"})

        self.assertEqual(200, status, payload)
        self.assertEqual("prescribed_confirmed", payload["source"])
        self.assertEqual("2026-08-10", payload["date"])
        squat = payload["movements"][0]["report"]
        self.assertEqual("back squat", squat["exercise"])
        self.assertEqual([70.0] * 4, [item["weight_kg"] for item in squat["sets"]])

        # A later conversation reads it back as strength_execution, naming what it is.
        _, session = self.session()
        group = session["context"]["strength_execution"]
        by_exercise = {item["exercise"]: item for item in group["sessions"]}
        self.assertEqual("prescribed_confirmed", by_exercise["back squat"]["source"])
        self.assertEqual(4, len(by_exercise["back squat"]["sets"]))

    def test_a_deviation_is_carried_and_the_rest_stays_prescribed(self):
        status, payload = self.prescribed(
            {
                "session_id": "strength-full-01",
                "deviations": [{"exercise": "back squat", "set": 4, "reps": 4}],
            }
        )

        self.assertEqual(200, status, payload)
        squat = payload["movements"][0]["report"]
        self.assertEqual([6, 6, 6, 4], [item["reps"] for item in squat["sets"]])

    def test_a_session_the_current_plan_does_not_hold_is_a_malformed_request(self):
        # Not a conflict: the plan is fine and the caller is looking at the wrong session,
        # so the fix is to read the plan again rather than to resolve anything.
        status, payload = self.prescribed({"session_id": "strength-does-not-exist"})

        self.assertEqual(400, status, payload)
        self.assertEqual("invalid_request", payload["error"])

    def test_a_running_session_cannot_be_confirmed_as_prescribed_strength(self):
        status, payload = self.prescribed({"session_id": "run-quality-01"})

        self.assertEqual(400, status, payload)
        self.assertEqual("invalid_request", payload["error"])

    def test_a_malformed_statement_is_refused_and_stores_nothing(self):
        cases = (
            ("/v1/coach/availability", {}),
            ("/v1/coach/availability", {"recurring": {"available_days": ["someday"]}}),
            ("/v1/coach/availability", {"recurring": {"available_days": []}}),
            # A week that has already ended is not a week anyone can plan.
            ("/v1/coach/availability", {"week": {"week_start": "2026-08-03", "available_days": ["tue"]}}),
            # The two forms answer different questions; together they have no meaning.
            ("/v1/coach/availability", {"week": {"only_days": ["tue"], "unavailable_days": ["wed"]}}),
            ("/v1/coach/availability", {"recurring": {"available_days": ["mon"]}, "recuring": {}}),
            # A movement with no sets reports nothing that was not already known.
            ("/v1/coach/strength-report", {"date": "2026-08-12", "exercise": "bench press"}),
            # A day the athlete has not reached yet.
            ("/v1/coach/strength-report", {
                "date": "2026-08-20", "exercise": "bench press", "sets": [{"set": 1}],
            }),
        )
        for path, body in cases:
            with self.subTest(path=path, body=body):
                status, payload = self.call("POST", path, body=body, token=TOKEN_A)
                self.assertEqual(400, status)
                self.assertEqual("invalid_request", payload["error"])
        self.assertFalse((self.state_dir / "athlete-evidence.json").exists())

    def test_neither_route_answers_without_a_token(self):
        for path in ("/v1/coach/availability", "/v1/coach/strength-report"):
            with self.subTest(path=path):
                status, payload = self.call("POST", path, body={}, token=None)
                self.assertEqual(401, status)
                self.assertEqual("unauthorized", payload["error"])
        # Refused before the body was parsed, so nothing was stored either.
        self.assertFalse((self.state_dir / "athlete-evidence.json").exists())

    # -- continuity across conversations -----------------------------------------------

    def test_a_week_stated_in_one_conversation_answers_the_next_one(self):
        self.availability(
            {"week": {"week_start": "2026-08-10", "only_days": ["tue", "thu", "sat"]}}
        )

        status, session = self.session()

        self.assertEqual(200, status)
        constraints = session["context"]["constraints"]
        self.assertEqual(["tue", "thu", "sat"], constraints["available_days"])
        self.assertEqual("athlete_evidence", constraints["availability_source"])
        self.assertNotIn("available_days_not_confirmed", session["unknowns"])

    def test_a_single_week_statement_stops_answering_once_that_week_is_over(self):
        self.availability({"recurring": {"available_days": ["mon", "wed", "fri"]}})
        self.availability(
            {"week": {"week_start": "2026-08-10", "only_days": ["tue", "thu", "sat"]}}
        )
        self.assertEqual(
            ["tue", "thu", "sat"], self.session()[1]["context"]["constraints"]["available_days"]
        )

        # A week later, without anyone deleting or expiring anything: the statement was
        # about one week, and that week is no longer the one being asked about.
        self.now = NOW + dt.timedelta(days=7)

        constraints = self.session()[1]["context"]["constraints"]
        self.assertEqual(["mon", "wed", "fri"], constraints["available_days"])
        self.assertEqual("athlete_evidence", constraints["availability_source"])

    def test_a_reported_lift_reaches_the_next_sessions_strength_evidence(self):
        self.strength(
            {
                "date": "2026-08-12",
                "exercise": "bench press",
                "category": "chest",
                "sets": [{"set": 1, "weight_kg": 65, "reps": 4}],
                "notes": ["最後一組沒做完"],
            }
        )

        group = self.session()[1]["context"]["strength_execution"]

        # Hosted builds never read a local health.db, so before this the group was
        # permanently null and the coach judged lifting from duration and average HR.
        self.assertEqual("athlete_reported", group["source"])
        self.assertEqual(1, len(group["sessions"]))
        self.assertEqual("bench press", group["sessions"][0]["exercise"])
        self.assertEqual(65, group["sessions"][0]["sets"][0]["weight_kg"])
        self.assertEqual(["最後一組沒做完"], group["sessions"][0]["notes"])

    def test_one_athletes_statements_never_appear_in_anothers_session(self):
        other_owner = self.seed_owner(TOKEN_B, athlete_id="i2", plan=publishable_plan())
        self.availability({"recurring": {"available_days": ["mon", "wed", "fri"]}})
        self.strength(
            {
                "date": "2026-08-12",
                "exercise": "bench press",
                "category": "chest",
                "sets": [{"set": 1, "weight_kg": 65, "reps": 4}],
            }
        )

        _, other_session = self.session(token=TOKEN_B)

        constraints = other_session["context"]["constraints"]
        self.assertEqual([], constraints["available_days"])
        self.assertIsNone(constraints["availability_source"])
        self.assertIsNone(other_session["context"]["strength_execution"])
        self.assertFalse((self.owner_dir(other_owner) / "athlete-evidence.json").exists())


class OneSentenceIsOneCallTests(GatewayTestCase):
    """What the athlete actually says, and what it costs them to say it.

    Every test here starts from a sentence a person would speak out loud and asserts two
    things about it: that it lands in a single call, and that what comes back is complete
    enough that the coach has nothing left to ask. The second half is the one that bites.
    A route can accept a sentence perfectly and still ruin the conversation, if what it
    stores forces the next question -- "and Monday?", "is bench chest?", "which set number
    was that?" -- and each of those questions is the product asking the athlete to fill in
    a form it could have filled in itself.

    So these assert on the *absence* of things too: no follow-up field, no unknown left
    standing, no day the athlete never mentioned turning up as unconfirmed.
    """

    def setUp(self):
        super().setUp()
        self.owner_id = self.seed_owner(TOKEN_A, plan=publishable_plan())
        self.state_dir = self.owner_dir(self.owner_id)
        self.calls = 0

    def say(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        """One thing the athlete said, as one call. Counts, so a test can assert the cost."""
        self.calls += 1
        status, payload = self.call("POST", path, body=body, token=TOKEN_A)
        self.assertEqual(200, status, payload)
        return payload

    def availability(self, body: dict[str, Any]) -> dict[str, Any]:
        return self.say("/v1/coach/availability", body)

    def strength(self, body: dict[str, Any]) -> dict[str, Any]:
        return self.say("/v1/coach/strength-report", body)

    def constraints(self) -> dict[str, Any]:
        status, payload = self.call("POST", "/v1/coach/session", body={}, token=TOKEN_A)
        self.assertEqual(200, payload and status, payload)
        return payload["context"]["constraints"]

    def sessions(self) -> list[dict[str, Any]]:
        _, payload = self.call("POST", "/v1/coach/session", body={}, token=TOKEN_A)
        group = payload["context"]["strength_execution"]
        return list(group["sessions"]) if group else []

    # -- strength ----------------------------------------------------------------------

    def test_today_i_benched_65_for_4(self):
        """"今天臥推 65kg，4 下" -- the movement and the numbers, and nothing else.

        No date (it is today), no category (bench press is bench press), no set number
        (there was one set). Each of those, required, is one more question the coach has
        to put to someone who already told it what happened.
        """
        stored = self.strength(
            {"exercise": "bench press", "sets": [{"weight_kg": 65, "reps": 4}]}
        )

        self.assertEqual(1, self.calls)
        self.assertEqual("2026-08-13", stored["report"]["date"])
        self.assertIsNone(stored["report"]["category"])
        self.assertEqual(1, stored["report"]["sets"][0]["set"])

        session = self.sessions()[0]
        self.assertEqual(("bench press", "2026-08-13"), (session["exercise"], session["date"]))
        self.assertEqual([65], [item["weight_kg"] for item in session["sets"]])
        self.assertEqual("athlete_reported", session["source"])

    def test_sorry_it_was_70_not_65(self):
        """A correction is the same session described again, never a second session.

        This is the one that was actually broken: appending left the coach reading 65 and
        70 as two sets, so an athlete correcting themselves doubled the volume on record.
        """
        self.strength({"exercise": "bench press", "sets": [{"weight_kg": 65, "reps": 4}]})
        corrected = self.strength({"exercise": "bench press", "sets": [{"weight_kg": 70, "reps": 4}]})

        self.assertEqual(2, self.calls)
        self.assertEqual(1, corrected["report_count"])
        self.assertEqual(65, corrected["replaced"]["sets"][0]["weight_kg"])

        sessions = self.sessions()
        self.assertEqual(1, len(sessions))
        self.assertEqual([70], [item["weight_kg"] for item in sessions[0]["sets"]])

    def test_a_correction_lands_however_the_movement_was_spelled(self):
        """The athlete does not re-type the movement identically to correct it."""
        self.strength({"exercise": "bench_press", "sets": [{"weight_kg": 65, "reps": 4}]})
        self.strength({"exercise": "Bench Press", "sets": [{"weight_kg": 70, "reps": 4}]})

        sessions = self.sessions()
        self.assertEqual(1, len(sessions))
        self.assertEqual([70], [item["weight_kg"] for item in sessions[0]["sets"]])

    def test_two_movements_on_one_day_are_two_records(self):
        """Replacing is per movement. A chest day is not one row."""
        self.strength({"exercise": "bench press", "sets": [{"weight_kg": 65, "reps": 4}]})
        self.strength({"exercise": "incline press", "sets": [{"weight_kg": 40, "reps": 8}]})

        self.assertEqual(
            ["bench press", "incline press"], sorted(item["exercise"] for item in self.sessions())
        )

    # -- availability ------------------------------------------------------------------

    def test_something_came_up_wednesday(self):
        """"這週三有事不能練", with Mon/Wed/Fri already standing.

        Monday and Friday were never in question and must not become questions. Before
        this, the week statement replaced the whole week: the athlete lost Wednesday and
        the coach lost Monday and Friday with it, then had to ask for both back.
        """
        self.availability({"recurring": {"available_days": ["mon", "wed", "fri"]}})
        answer = self.availability({"week": {"unavailable_days": ["wed"]}})

        self.assertEqual(2, self.calls)
        effective = answer["effective_this_week"]
        self.assertEqual(["mon", "fri"], effective["available_days"])
        self.assertEqual(["wed"], effective["unavailable_days"])

        constraints = self.constraints()
        self.assertEqual(["mon", "fri"], constraints["available_days"])
        self.assertEqual(["wed"], constraints["unavailable_days"])

    def test_saturday_is_free_too(self):
        """"週六也有空" adds a day without disturbing the standing week."""
        self.availability({"recurring": {"available_days": ["mon", "wed", "fri"]}})
        answer = self.availability({"week": {"available_days": ["sat"]}})

        self.assertEqual(
            ["mon", "wed", "fri", "sat"], answer["effective_this_week"]["available_days"]
        )
        self.assertEqual([], answer["effective_this_week"]["unavailable_days"])

    def test_this_week_i_can_only_do_tuesday_and_thursday(self):
        """"這週只有二、四可以" -- the word "only" is what makes this the whole week."""
        self.availability({"recurring": {"available_days": ["mon", "wed", "fri"]}})
        answer = self.availability({"week": {"only_days": ["tue", "thu"]}})

        effective = answer["effective_this_week"]
        self.assertEqual(["tue", "thu"], effective["available_days"])
        self.assertEqual(["mon", "wed", "fri"], effective["unavailable_days"])

        # And only this week. Next week the standing days are back untouched.
        self.now = NOW + dt.timedelta(days=7)
        self.assertEqual(["mon", "wed", "fri"], self.constraints()["available_days"])

    def test_from_now_on_i_train_tuesday_thursday_saturday(self):
        """"以後改成二四六" moves the standing week itself, not one week of it."""
        self.availability({"recurring": {"available_days": ["mon", "wed", "fri"]}})
        answer = self.availability({"recurring": {"available_days": ["tue", "thu", "sat"]}})

        self.assertEqual(["tue", "thu", "sat"], answer["effective_this_week"]["available_days"])
        self.now = NOW + dt.timedelta(days=7)
        self.assertEqual(["tue", "thu", "sat"], self.constraints()["available_days"])

    def test_two_things_came_up_in_two_turns(self):
        """Said one at a time, they compose. Said together, they would mean the same."""
        self.availability({"recurring": {"available_days": ["mon", "wed", "fri"]}})
        self.availability({"week": {"unavailable_days": ["wed"]}})
        answer = self.availability({"week": {"unavailable_days": ["fri"]}})

        self.assertEqual(["mon"], answer["effective_this_week"]["available_days"])
        self.assertEqual(["wed", "fri"], answer["effective_this_week"]["unavailable_days"])

    def test_wednesday_is_back_on(self):
        """A day taken away and given back leaves no trace of having been taken."""
        self.availability({"recurring": {"available_days": ["mon", "wed", "fri"]}})
        self.availability({"week": {"unavailable_days": ["wed"]}})
        answer = self.availability({"week": {"available_days": ["wed"]}})

        self.assertEqual(["mon", "wed", "fri"], answer["effective_this_week"]["available_days"])
        self.assertEqual([], answer["effective_this_week"]["unavailable_days"])


class PrePlanObservationTests(GatewayTestCase):
    """What a first conversation should not have to ask for (#28).

    An account with no plan still has an Intervals history and may already have reported
    availability. Re-asking for either collects a worse answer than the record holds, in
    the one turn where the athlete is deciding whether this is worth using.
    """

    def setUp(self):
        super().setUp()
        # Identity only: authenticated, with no store of any kind.
        self.owner_id = self.seed_owner(TOKEN_A)
        self.state_dir = self.owner_dir(self.owner_id)
        self.fake.activities = [
            {
                "id": "i3001",
                "type": "Run",
                "start_date_local": "2026-08-11T07:00:00",
                "moving_time": 1800,
                "distance": 4200.0,
                "average_speed": 2.33,
                "average_heartrate": 149,
            }
        ]

    def test_an_empty_account_reports_the_training_the_provider_already_holds(self):
        status, payload = self.call("POST", "/v1/coach/session", body={}, token=TOKEN_A)

        self.assertEqual(200, status)
        self.assertEqual("no_plan_state", payload["status"])
        observations = payload["pre_plan_observations"]
        self.assertIsNone(observations["athlete_evidence"])
        self.assertEqual(
            ["intervals:i3001"],
            [item["activity_id"] for item in observations["recent_training"]["recent_actuals"]],
        )
        self.assertEqual("2026-08-13", observations["recent_training"]["window_end"])
        self.assertIn("status", observations["recent_training"]["coverage_activities"])
        # Reading is not writing: the account still has no store.
        self.assertFalse(self.state_dir.exists())

    def test_a_provider_that_cannot_be_read_lowers_the_answer_without_blocking_it(self):
        self.fake.read_status = 500

        status, payload = self.call("POST", "/v1/coach/session", body={}, token=TOKEN_A)

        self.assertEqual(200, status)
        self.assertEqual("no_plan_state", payload["status"])
        self.assertIsNone(payload["pre_plan_observations"]["recent_training"])
        self.assertIn("no PlanState exists for this account", payload["unknowns"])
        self.assertTrue(
            [note for note in payload["unknowns"] if note.startswith("recent_training unavailable")],
            payload["unknowns"],
        )

    def test_availability_reported_before_any_plan_is_read_back_before_asking(self):
        self.call(
            "POST",
            "/v1/coach/availability",
            body={"recurring": {"available_days": ["mon", "wed", "fri"]}},
            token=TOKEN_A,
        )

        _, payload = self.call("POST", "/v1/coach/session", body={}, token=TOKEN_A)

        evidence = payload["pre_plan_observations"]["athlete_evidence"]
        self.assertEqual(["mon", "wed", "fri"], evidence["availability"]["recurring"]["available_days"])
        self.assertEqual(
            ["mon", "wed", "fri"], evidence["availability"]["effective_this_week"]["available_days"]
        )
        self.assertEqual([], evidence["strength_reports"])

    def test_lifts_reported_before_any_plan_arrive_whole_not_as_a_count(self):
        self.call(
            "POST",
            "/v1/coach/strength-report",
            body={
                "date": "2026-08-12",
                "exercise": "bench press",
                "category": "chest",
                "sets": [{"set": 1, "weight_kg": 65, "reps": 4}],
            },
            token=TOKEN_A,
        )

        _, payload = self.call("POST", "/v1/coach/session", body={}, token=TOKEN_A)

        reports = payload["pre_plan_observations"]["athlete_evidence"]["strength_reports"]
        self.assertEqual(1, len(reports))
        self.assertEqual(65, reports[0]["sets"][0]["weight_kg"])

    def test_an_account_that_already_has_a_plan_carries_no_such_field(self):
        self.seed_owner(TOKEN_B, athlete_id="i2", plan=publishable_plan())

        _, payload = self.call("POST", "/v1/coach/session", body={}, token=TOKEN_B)

        self.assertEqual("passed", payload["status"])
        self.assertNotIn("pre_plan_observations", payload)


class EndToEndLoopTests(GatewayTestCase):
    """The whole loop, once, over the transport the athlete actually uses.

    Read the latest evidence, reconcile, change the week and confirm it, deliver and have
    the provider read back exactly, start a fresh session and see the new version, move a
    session that was already delivered, and withdraw one that no longer has anything to
    deliver. Every hop goes through the loopback server against the injected provider, so
    what is asserted is what a real caller would see -- no direct function calls into the
    modules under test.
    """

    def setUp(self):
        super().setUp()
        self.plan = publishable_plan()
        self.owner_id = self.seed_owner(TOKEN_A, plan=self.plan)
        self.state_dir = self.owner_dir(self.owner_id)
        # The loop delivers a heart-rate ceiling, which Intervals resolves against the
        # account's Run threshold HR -- so this account has one, as a real one does.
        self.fake.sport_settings = RUN_SPORT_SETTINGS

    def session(self) -> dict[str, Any]:
        status, payload = self.call("POST", "/v1/coach/session", body={}, token=TOKEN_A)
        self.assertEqual(200, status, payload)
        return payload

    def decide(self, change_request: dict[str, Any]) -> dict[str, Any]:
        """One coaching change, prepared and then confirmed -- the two-call contract."""
        current = self.session()
        body = {
            "plan_id": current["plan_state"]["plan_id"],
            "plan_version": current["plan_state"]["plan_version"],
            "context": current["context"],
            "change_request": change_request,
        }
        status, prepared = self.call(
            "POST", "/v1/coach/decision/prepare", body=body, token=TOKEN_A
        )
        self.assertEqual(200, status, prepared)
        status, applied = self.call(
            "POST",
            "/v1/coach/decision/apply",
            body={**body, "proposal": prepared["proposal"], "confirmed": True},
            token=TOKEN_A,
        )
        self.assertEqual(200, status, applied)
        # A confirmed change is a workout the provider has not seen before, so the fake
        # learns what its text parses into -- exactly as Intervals would on the write.
        self.fake.register_plan_steps(read_current_plan(self.state_dir)["current_plan"])
        return applied

    def deliver(self, session_ids: list[str]) -> dict[str, Any]:
        current = self.session()
        status, prepared = self.call(
            "POST",
            "/v1/coach/delivery/prepare",
            body={
                "plan_id": current["plan_state"]["plan_id"],
                "plan_version": current["plan_state"]["plan_version"],
                "session_ids": session_ids,
            },
            token=TOKEN_A,
        )
        self.assertEqual(200, status, prepared)
        status, published = self.call(
            "POST",
            "/v1/coach/delivery/publish",
            body={
                "delivery_set": prepared["delivery_set"],
                "proposal_hash": prepared["proposal_hash"],
                "confirmed": True,
            },
            token=TOKEN_A,
        )
        self.assertEqual(200, status, published)
        return published

    def delivery_view(self, session_id: str) -> dict[str, Any]:
        return next(
            item
            for item in self.session()["delivery"]["sessions"]
            if item["session_id"] == session_id
        )

    def test_the_whole_loop_stays_consistent_from_evidence_to_calendar(self):
        opened = self.session()
        self.assertEqual(1, opened["plan_state"]["plan_version"])
        self.assertEqual("passed", opened["reconciliation"]["status"])
        self.assertEqual(
            "not_published", self.delivery_view("run-quality-01")["delivery_state"]
        )

        # 1. Change the week and confirm it.
        applied = self.decide(WEEKLY_CHANGE)
        self.assertEqual(2, applied["plan_version"])

        # 2. Deliver, with the provider read back exactly.
        published = self.deliver(["run-quality-01"])
        self.assertEqual("intervals_accepted", published["delivery_state"])
        self.assertEqual([], published["unresolved"])
        delivered_id = published["delivered"][0]["external_id"]
        self.assertEqual(1, len(self.fake.events))

        # 3. A fresh session -- a new conversation -- reads the new version and the
        #    delivery it can actually observe.
        reopened = self.session()
        self.assertEqual(3, reopened["plan_state"]["plan_version"])
        view = self.delivery_view("run-quality-01")
        self.assertEqual("intervals_accepted", view["delivery_state"])
        self.assertEqual(delivered_id, view["external_id"])
        self.assertIsNone(view["superseded_external_id"])

        # 4. Move the session that was already delivered. The old event is now recorded
        #    as outstanding rather than silently forgotten.
        self.decide(
            {
                "summary": "把這堂課移到週六",
                "reason_codes": ["schedule_or_equipment_changed"],
                "evidence": [{"field": "constraints", "observation": "週四臨時有事"}],
                "goal_effect": {"week": "同一堂課換一天", "cycle": "28 天方向不變"},
                "next_review_condition": "移動後重新確認交付",
                "sessions": [
                    {
                        "operation": "move",
                        "session_id": "run-quality-01",
                        "scheduled_date": "2026-08-15",
                    }
                ],
            }
        )
        moved = self.delivery_view("run-quality-01")
        self.assertEqual("not_published", moved["delivery_state"])
        self.assertEqual(delivered_id, moved["superseded_external_id"])

        # 5. Re-delivering replaces that same event rather than adding a second one.
        self.deliver(["run-quality-01"])
        self.assertEqual(1, len(self.fake.events))
        self.assertEqual("2026-08-15", str(self.fake.events[0]["start_date_local"])[:10])
        self.assertEqual(delivered_id, str(self.fake.events[0]["id"]))
        redelivered = self.delivery_view("run-quality-01")
        self.assertEqual("intervals_accepted", redelivered["delivery_state"])
        self.assertIsNone(redelivered["superseded_external_id"])

        # 6. Replace it with something that cannot be delivered at all, and withdraw the
        #    event the athlete would otherwise still be following.
        self.decide(
            {
                "summary": "改成完全休息",
                "reason_codes": ["multi_signal_recovery_down"],
                "evidence": [{"field": "recovery_trends.hrv", "observation": "HRV 連三天偏低"}],
                "goal_effect": {"week": "本週少一次刺激", "cycle": "28 天方向不變"},
                "next_review_condition": "休息後重新評估",
                "sessions": [
                    {
                        "operation": "replace",
                        "session_id": "run-quality-01",
                        "sport": "rest",
                        "purpose": "完全休息",
                        "adaptation": "recovery",
                        "cost": "easy",
                        "planned_minutes": 0,
                        "plan": {"kind": "unstructured"},
                    }
                ],
            }
        )
        superseded = self.delivery_view("run-quality-01")["superseded_external_id"]
        self.assertEqual(delivered_id, superseded)

        current = self.session()
        status, prepared = self.call(
            "POST",
            "/v1/coach/delivery/withdraw/prepare",
            body={
                "plan_id": current["plan_state"]["plan_id"],
                "plan_version": current["plan_state"]["plan_version"],
                "session_ids": ["run-quality-01"],
            },
            token=TOKEN_A,
        )
        self.assertEqual(200, status, prepared)
        status, withdrawn = self.call(
            "POST",
            "/v1/coach/delivery/withdraw/apply",
            body={
                "withdrawal_set": prepared["withdrawal_set"],
                "proposal_hash": prepared["proposal_hash"],
                "confirmed": True,
            },
            token=TOKEN_A,
        )
        self.assertEqual(200, status, withdrawn)

        # 7. Plan, calendar and what the athlete is shown all say the same thing.
        self.assertEqual([], self.fake.events)
        final = self.delivery_view("run-quality-01")
        self.assertEqual("not_published", final["delivery_state"])
        self.assertIsNone(final["external_id"])
        self.assertIsNone(final["superseded_external_id"])
        self.assertEqual("passed", self.session()["reconciliation"]["status"])

    def test_a_set_that_fails_halfway_is_recoverable_without_writing_anything_twice(self):
        current = self.session()
        status, prepared = self.call(
            "POST",
            "/v1/coach/delivery/prepare",
            body={
                "plan_id": current["plan_state"]["plan_id"],
                "plan_version": current["plan_state"]["plan_version"],
                "session_ids": ["run-quality-01", "run-long-01"],
            },
            token=TOKEN_A,
        )
        self.assertEqual(200, status, prepared)
        # The provider accepts the second write but echoes a workout that is not the one
        # that was approved.
        second_owned_id = prepared["preview"][1]["owned_external_id"]
        self.fake.corrupt_external_ids.add(second_owned_id)

        status, published = self.call(
            "POST",
            "/v1/coach/delivery/publish",
            body={
                "delivery_set": prepared["delivery_set"],
                "proposal_hash": prepared["proposal_hash"],
                "confirmed": True,
            },
            token=TOKEN_A,
        )

        self.assertEqual(200, status, published)
        self.assertEqual("partial", published["status"])
        self.assertEqual(1, len(published["delivered"]))
        first_session = prepared["preview"][0]["session_id"]
        second_session = prepared["preview"][1]["session_id"]
        self.assertEqual(first_session, published["delivered"][0]["session_id"])
        self.assertEqual(
            [second_session], [item["session_id"] for item in published["unresolved"]]
        )
        # What Intervals accepted is recorded; what it did not is not claimed.
        self.assertEqual(
            "intervals_accepted", self.delivery_view(first_session)["delivery_state"]
        )
        self.assertEqual(
            "not_published", self.delivery_view(second_session)["delivery_state"]
        )
        writes_before_retry = len(self.fake.bulk_calls)
        # The write that landed and did not verify is a provider effect nothing has
        # reconciled, so the reservation stays and says so on every surface (issue #121).
        self.assertTrue(published["attempt_open"])
        outstanding = self.session()["delivery"]["unresolved_delivery"]
        self.assertEqual(
            [second_session], [item["session_id"] for item in outstanding["operations"]]
        )
        self.assertEqual("mutated_unverified", outstanding["operations"][0]["state"])

        # A freshly bound delivery is refused while that is outstanding; the retry is the
        # same confirmed set, which converges rather than repeats.
        status, refused = self.call(
            "POST",
            "/v1/coach/delivery/prepare",
            body={
                "plan_id": self.session()["plan_state"]["plan_id"],
                "plan_version": self.session()["plan_state"]["plan_version"],
                "session_ids": [second_session],
            },
            token=TOKEN_A,
        )
        self.assertEqual(200, status, refused)
        status, blocked = self.call(
            "POST",
            "/v1/coach/delivery/publish",
            body={
                "delivery_set": refused["delivery_set"],
                "proposal_hash": refused["proposal_hash"],
                "confirmed": True,
            },
            token=TOKEN_A,
        )
        self.assertEqual(409, status, blocked)

        self.fake.corrupt_external_ids.discard(second_owned_id)
        status, retried = self.call(
            "POST",
            "/v1/coach/delivery/publish",
            body={
                "delivery_set": prepared["delivery_set"],
                "proposal_hash": prepared["proposal_hash"],
                "confirmed": True,
            },
            token=TOKEN_A,
        )

        self.assertEqual(200, status, retried)
        self.assertEqual("passed", retried["status"])
        self.assertFalse(retried["attempt_open"])
        self.assertEqual(
            sorted([first_session, second_session]),
            sorted(item["session_id"] for item in retried["delivered"]),
        )
        # The session that already landed is never written a second time; the one that
        # did not is corrected in place, so no second event appears for it.
        first_owned_id = prepared["preview"][0]["owned_external_id"]
        self.assertEqual(
            1,
            len([call for call in self.fake.bulk_calls if call["external_id"] == first_owned_id]),
        )
        self.assertGreaterEqual(len(self.fake.bulk_calls), writes_before_retry)
        self.assertEqual(
            {"intervals_accepted", "intervals_accepted"},
            {
                self.delivery_view(first_session)["delivery_state"],
                self.delivery_view(second_session)["delivery_state"],
            },
        )
        self.assertEqual(2, len(self.fake.events))
        self.assertIsNone(self.session()["delivery"]["unresolved_delivery"])


class InterruptedDeliveryRecoveryTests(GatewayTestCase):
    """Issue #16: the conversation that could retry the delivery is gone. Now what?

    Retrying converges without writing twice, but the confirmed set exists only inside the
    conversation that prepared it. When that conversation ends, the athlete is left with a
    store that refuses every plan change and a recovery command on a machine they have no
    access to. These tests are the hosted way out: keep reading, and clear on purpose.
    """

    def setUp(self):
        super().setUp()
        self.plan = publishable_plan()
        self.owner_id = self.seed_owner(TOKEN_A, plan=self.plan)
        self.state_dir = self.owner_dir(self.owner_id)

    # -- helpers ----------------------------------------------------------------------

    def session(self, *, token: str = TOKEN_A) -> dict[str, Any]:
        status, payload = self.call("POST", "/v1/coach/session", body={}, token=token)
        self.assertEqual(200, status, payload)
        return payload

    def interrupt(self, *, token: str = TOKEN_A) -> str:
        """Leave the store exactly as a delivery killed after one provider write does.

        The first session lands and verifies; the second is accepted by Intervals and
        reads back as something else, so its effect is real and unreconciled. That is the
        state the reservation exists to describe.
        """
        current = self.session(token=token)
        status, prepared = self.call(
            "POST",
            "/v1/coach/delivery/prepare",
            body={
                "plan_id": current["plan_state"]["plan_id"],
                "plan_version": current["plan_state"]["plan_version"],
                "session_ids": ["run-quality-01", "run-long-01"],
            },
            token=token,
        )
        self.assertEqual(200, status, prepared)
        self.fake.corrupt_external_ids.add(prepared["preview"][1]["owned_external_id"])
        status, published = self.call(
            "POST",
            "/v1/coach/delivery/publish",
            body={
                "delivery_set": prepared["delivery_set"],
                "proposal_hash": prepared["proposal_hash"],
                "confirmed": True,
            },
            token=token,
        )
        self.assertEqual(200, status, published)
        self.assertTrue(published["attempt_open"])
        return published["delivered"][0]["external_id"]

    def clear(self, attempt_id: Any, *, token: str = TOKEN_A, **overrides: Any):
        body: dict[str, Any] = {"attempt_id": attempt_id, "confirmed": True}
        body.update(overrides)
        return self.call(
            "POST", "/v1/coach/delivery/attempt/clear", body=body, token=token
        )

    def pair_an_actual(self, external_id: str) -> None:
        """One completed run the provider has already paired to a delivered session.

        A matched actual is what reconciliation writes for, so this is what turns the open
        reservation from "a block on changes" into "a block on reading" -- the failure the
        deferred path exists to remove.
        """
        self.fake.activities = [
            {
                "id": "i4001",
                "type": "Run",
                "start_date_local": "2026-08-13T07:00:00",
                "moving_time": 2400,
                "distance": 8000.0,
                "average_speed": 3.33,
                "average_heartrate": 158,
                "paired_event_id": external_id,
            }
        ]

    # -- the reservation is legible from a conversation that never saw it -------------

    def test_a_new_conversation_is_told_the_whole_reservation_and_nothing_else(self):
        self.interrupt()
        self.log_handler.records.clear()

        payload = self.session()
        outstanding = payload["delivery"]["unresolved_delivery"]

        self.assertEqual("delivery", outstanding["kind"])
        self.assertTrue(outstanding["attempt_id"])
        self.assertEqual(payload["plan_state"]["plan_id"], outstanding["plan_id"])
        # Every session the interrupted set covered, and separately the ones that still
        # need attention -- which are not the same list.
        self.assertEqual(["run-long-01", "run-quality-01"], outstanding["session_ids"])
        self.assertTrue(outstanding["provider_effects_outstanding"])
        self.assertEqual(
            [("run-long-01", "upsert", "mutated_unverified")],
            [
                (item["session_id"], item["operation"], item["state"])
                for item in outstanding["operations"]
            ],
        )
        self.assertEqual(
            ["retry_same_set", "clear_delivery_attempt"], outstanding["next_actions"]
        )
        # The plan is still fully readable next to it.
        self.assertEqual("passed", payload["status"])
        self.assertIsNotNone(payload["plan_state"]["current_plan"])

        blob = json.dumps(payload, ensure_ascii=False) + "\n".join(self.log_handler.records)
        for secret in (TOKEN_A, self.owner_id, str(self.state_root), CLIENT_SECRET_VALUE):
            self.assertNotIn(secret, blob)

    def test_the_session_stays_readable_when_an_actual_would_otherwise_be_reconciled(self):
        """The acceptance criterion the deferral exists for.

        Before this, an interrupted delivery plus one completed session was a session that
        failed outright: reconciliation tried to commit, the reservation refused the
        commit, and the athlete could not even read the plan they were being blocked on.
        """
        delivered_id = self.interrupt()
        self.pair_an_actual(delivered_id)

        payload = self.session()

        self.assertEqual("passed", payload["status"])
        reconciliation = payload["reconciliation"]
        self.assertEqual("deferred", reconciliation["status"])
        self.assertEqual("unresolved_delivery_attempt", reconciliation["reason"])
        self.assertEqual([], reconciliation["applied"])
        self.assertEqual(
            payload["delivery"]["unresolved_delivery"]["attempt_id"],
            reconciliation["attempt_id"],
        )
        # Deferred means "not written", not "already true": the session it would have
        # reconciled is still reported exactly as the plan holds it.
        quality = next(
            item
            for item in payload["delivery"]["sessions"]
            if item["session_id"] == "run-quality-01"
        )
        self.assertEqual("planned", quality["match_status"])

        status, cleared = self.clear(
            payload["delivery"]["unresolved_delivery"]["attempt_id"]
        )
        self.assertEqual(200, status, cleared)

        # And the deferral was only a deferral: the very next session reconciles the
        # actual that was waiting all along.
        resumed = self.session()
        self.assertEqual("passed", resumed["reconciliation"]["status"])
        self.assertEqual(
            ["run-quality-01"],
            [item["session_id"] for item in resumed["reconciliation"]["applied"]],
        )
        self.assertIsNone(resumed["delivery"]["unresolved_delivery"])

    # -- clearing is bound, confirmed, and owned --------------------------------------

    def test_clearing_names_the_reservation_it_abandons(self):
        self.interrupt()
        attempt_id = self.session()["delivery"]["unresolved_delivery"]["attempt_id"]
        self.log_handler.records.clear()

        status, payload = self.clear(attempt_id)

        self.assertEqual(200, status, payload)
        self.assertTrue(payload["cleared"])
        self.assertEqual(attempt_id, payload["attempt_id"])
        self.assertEqual(
            [("run-long-01", "upsert", "mutated_unverified")],
            [
                (item["session_id"], item["operation"], item["state"])
                for item in payload["abandoned"]
            ],
        )
        self.assertIn("Intervals calendar", payload["detail"])
        self.assertIsNone(self.session()["delivery"]["unresolved_delivery"])

        blob = json.dumps(payload, ensure_ascii=False) + "\n".join(self.log_handler.records)
        for secret in (TOKEN_A, self.owner_id, str(self.state_root)):
            self.assertNotIn(secret, blob)

    def test_a_confirmation_that_names_another_reservation_clears_nothing(self):
        self.interrupt()
        outstanding = self.session()["delivery"]["unresolved_delivery"]

        status, payload = self.clear("delivery-attempt-something-else")

        self.assertEqual(409, status, payload)
        self.assertEqual("attempt_mismatch", payload["error"])
        # The response says which one is actually open, so the next turn can be right.
        self.assertEqual(
            outstanding["attempt_id"], payload["unresolved_delivery"]["attempt_id"]
        )
        self.assertEqual(
            outstanding, self.session()["delivery"]["unresolved_delivery"]
        )

    def test_clearing_without_an_explicit_confirmation_is_refused(self):
        self.interrupt()
        attempt_id = self.session()["delivery"]["unresolved_delivery"]["attempt_id"]

        for confirmation in ({}, {"confirmed": False}, {"confirmed": "true"}):
            body = {"attempt_id": attempt_id, **confirmation}
            status, payload = self.call(
                "POST", "/v1/coach/delivery/attempt/clear", body=body, token=TOKEN_A
            )
            self.assertEqual(409, status, payload)
            self.assertEqual("confirmation_required", payload["error"], confirmation)

        status, payload = self.call(
            "POST",
            "/v1/coach/delivery/attempt/clear",
            body={"confirmed": True},
            token=TOKEN_A,
        )
        self.assertEqual(400, status, payload)
        self.assertEqual("invalid_request", payload["error"])
        self.assertIsNotNone(self.session()["delivery"]["unresolved_delivery"])

    def test_one_athletes_token_cannot_clear_another_athletes_reservation(self):
        self.interrupt()
        attempt_id = self.session()["delivery"]["unresolved_delivery"]["attempt_id"]
        other_owner = self.seed_owner(TOKEN_B, athlete_id="i2", plan=publishable_plan())
        self.assertNotEqual(self.owner_id, other_owner)

        status, payload = self.clear(attempt_id, token=TOKEN_B)

        # The owner comes from the token, so B's confirmation reaches B's own store --
        # which holds nothing -- and A's reservation is untouched.
        self.assertEqual(200, status, payload)
        self.assertFalse(payload["cleared"])
        self.assertIsNone(payload["attempt_id"])
        self.assertEqual(
            attempt_id, self.session()["delivery"]["unresolved_delivery"]["attempt_id"]
        )

    def test_clearing_a_reservation_that_is_already_gone_is_not_an_error(self):
        self.interrupt()
        attempt_id = self.session()["delivery"]["unresolved_delivery"]["attempt_id"]

        status, first = self.clear(attempt_id)
        self.assertEqual(200, status, first)
        self.assertTrue(first["cleared"])

        status, second = self.clear(attempt_id)

        self.assertEqual(200, status, second)
        self.assertFalse(second["cleared"])
        self.assertIsNone(second["attempt_id"])
        self.assertEqual([], second["abandoned"])
        self.assertIn("nothing was cleared", second["detail"])

    def test_the_plan_can_be_changed_again_once_the_reservation_is_released(self):
        self.interrupt()
        current = self.session()
        body = {
            "plan_id": current["plan_state"]["plan_id"],
            "plan_version": current["plan_state"]["plan_version"],
            "context": current["context"],
            "change_request": copy.deepcopy(WEEKLY_CHANGE),
        }
        status, prepared = self.call(
            "POST", "/v1/coach/decision/prepare", body=body, token=TOKEN_A
        )
        self.assertEqual(200, status, prepared)
        status, refused = self.call(
            "POST",
            "/v1/coach/decision/apply",
            body={**body, "proposal": prepared["proposal"], "confirmed": True},
            token=TOKEN_A,
        )
        self.assertEqual(409, status, refused)
        self.assertEqual("state_conflict", refused["error"])

        attempt_id = current["delivery"]["unresolved_delivery"]["attempt_id"]
        status, cleared = self.clear(attempt_id)
        self.assertEqual(200, status, cleared)

        # The identical confirmation, refused a moment ago purely by the fence, now
        # commits: clearing restored writes and changed nothing else about the request.
        status, applied = self.call(
            "POST",
            "/v1/coach/decision/apply",
            body={**body, "proposal": prepared["proposal"], "confirmed": True},
            token=TOKEN_A,
        )
        self.assertEqual(200, status, applied)
        self.assertEqual(body["plan_version"] + 1, applied["plan_version"])


if __name__ == "__main__":
    unittest.main()

# --------------------------------------------------------------------------------------
# Two-athlete journey
# --------------------------------------------------------------------------------------


# A second athlete's own first plan, authored from zero history: no ``baselines`` object
# at all, which ``_athlete_baseline`` reads as every field unmeasured, plus one
# open-effort running session -- the minimum ``initialization_request`` accepts. Its
# vocabulary (goal wording, session id) is deliberately unlike anything in ``ONBOARDING``
# or ``plan-state-v1.json``, so a leak across owners would show up as a plain substring
# match rather than something that has to be reasoned about.
SECOND_ATHLETE_ONBOARDING: dict[str, Any] = {
    "goal": {
        "outcome": "四週後能不中斷慢跑 20 分鐘",
        "measurement_protocol": "第 28 天在跑道上連續跑,記錄中斷次數與總時間",
    },
    "cycle": {
        "start": "2026-08-10",
        "primary_adaptation": "aerobic_base",
        "planned_evidence": ["每週排定的跑走都完成"],
        "adjust_conditions": ["連續兩週有一次沒做到"],
        "stop_conditions": ["出現疼痛、生病或不尋常症狀時交給人判斷"],
    },
    "week_intent": "第一週先建立一次開放強度的有氧曝露",
    "sessions": [easy_run(scheduled_date="2026-08-14")],
    "summary": "全新開始,零歷史資料,先用開放強度的跑步建立節奏,基準之後再測",
    "evidence": [
        {"field": "athlete_reported", "observation": "剛開始訓練,沒有任何歷史紀錄"},
    ],
    "unknowns": ["還沒有任何量測基準"],
}


class TwoAthleteJourneyTests(GatewayTestCase):
    """One continuous timeline, not another point-in-time snapshot of the owner boundary.

    ``test_two_athletes_get_disjoint_owners_state_dirs_and_answers``,
    ``test_a_second_athlete_initializes_a_disjoint_store``,
    ``test_another_owners_proposal_is_refused_even_when_the_plan_ids_match`` and
    ``test_one_athletes_statements_never_appear_in_anothers_session`` each freeze one
    instant and check that the owner boundary holds there. None of them plays the whole
    story back: an athlete already mid-cycle stays in normal operation while a second
    athlete's entire onboarding happens in between, both athletes' calls interleaved in
    time, everything over the one loopback server and the one injected transport a real
    deployment shares across every athlete it serves. This is that story, once, start to
    finish.
    """

    def session_for(self, token: str) -> dict[str, Any]:
        status, payload = self.call("POST", "/v1/coach/session", body={}, token=token)
        self.assertEqual(200, status, payload)
        return payload

    def decide_for(self, token: str, change_request: dict[str, Any]) -> dict[str, Any]:
        """The two-call decision contract, against whichever athlete's token is given."""
        current = self.session_for(token)
        body = {
            "plan_id": current["plan_state"]["plan_id"],
            "plan_version": current["plan_state"]["plan_version"],
            "context": current["context"],
            "change_request": change_request,
        }
        status, prepared = self.call(
            "POST", "/v1/coach/decision/prepare", body=body, token=token
        )
        self.assertEqual(200, status, prepared)
        status, applied = self.call(
            "POST",
            "/v1/coach/decision/apply",
            body={**body, "proposal": prepared["proposal"], "confirmed": True},
            token=token,
        )
        self.assertEqual(200, status, applied)
        return applied

    def deliver_for(self, token: str, session_ids: list[str]) -> dict[str, Any]:
        current = self.session_for(token)
        status, prepared = self.call(
            "POST",
            "/v1/coach/delivery/prepare",
            body={
                "plan_id": current["plan_state"]["plan_id"],
                "plan_version": current["plan_state"]["plan_version"],
                "session_ids": session_ids,
            },
            token=token,
        )
        self.assertEqual(200, status, prepared)
        status, published = self.call(
            "POST",
            "/v1/coach/delivery/publish",
            body={
                "delivery_set": prepared["delivery_set"],
                "proposal_hash": prepared["proposal_hash"],
                "confirmed": True,
            },
            token=token,
        )
        self.assertEqual(200, status, published)
        return published

    def test_a_second_athletes_onboarding_interleaves_with_an_established_athletes_week(self):
        # 1. Athlete A is already mid-cycle: identity and a first plan both already exist,
        #    the way every ordinary conversation after the first one finds them.
        a_plan = publishable_plan()
        owner_a = self.seed_owner(TOKEN_A, athlete_id="i1", plan=a_plan)
        state_dir_a = self.owner_dir(owner_a)
        opened_a = self.session_for(TOKEN_A)
        self.assertEqual(a_plan["plan_id"], opened_a["plan_state"]["plan_id"])
        self.assertEqual(1, opened_a["plan_state"]["plan_version"])

        # 2. Athlete B completes OAuth for the first time, as a different Intervals
        #    athlete id. The exchange alone must produce a new, disjoint owner -- A's
        #    identity and directory are never touched by it.
        self.fake.token_payload = {
            "token_type": "Bearer",
            "access_token": TOKEN_B,
            "scope": "ACTIVITY:READ",
            "athlete": {"id": "i2", "name": "Second Athlete"},
        }
        status, exchanged = self.call(
            "POST",
            "/oauth/intervals/token",
            raw=urllib.parse.urlencode(
                {"grant_type": "authorization_code", "code": "second-athlete-c1"}
            ).encode("utf-8"),
            content_type="application/x-www-form-urlencoded",
        )
        self.assertEqual(200, status, exchanged)
        self.assertEqual(TOKEN_B, exchanged["access_token"])

        owner_b = owner_for_fingerprint(
            self.identity_db, token_fingerprint(TOKEN_B, hmac_key=HMAC_KEY)
        )
        self.assertIsNotNone(owner_b)
        self.assertNotEqual(owner_a, owner_b)
        state_dir_b = self.owner_dir(owner_b)
        self.assertNotEqual(state_dir_a, state_dir_b)

        # 3. B's very first session has no plan to read. Reading it must not create
        #    anything for B, and must not so much as touch A, whom this request never
        #    names.
        first_b_session = self.session_for(TOKEN_B)
        self.assertEqual("no_plan_state", first_b_session["status"])
        self.assertFalse(first_b_session["plan_state"]["present"])
        self.assertIsNone(first_b_session["plan_state"]["current_plan"])
        self.assertFalse(state_dir_b.exists())
        self.assertEqual(
            a_plan["plan_id"], self.session_for(TOKEN_A)["plan_state"]["plan_id"]
        )

        # 4. B builds a first plan from zero history: no measured baseline of any kind,
        #    one open-effort running session -- the minimum the contract accepts.
        status, prepared_b = self.call(
            "POST",
            "/v1/coach/initialization/prepare",
            body={"initialization_request": SECOND_ATHLETE_ONBOARDING},
            token=TOKEN_B,
        )
        self.assertEqual(200, status, prepared_b)
        self.assertEqual("passed", prepared_b["status"])
        baseline_b = prepared_b["preview"]["athlete_baseline"]
        self.assertTrue(
            all(
                baseline_b[name] is None
                for name in (
                    "threshold_pace_sec_per_km", "max_hr", "easy_hr_ceiling",
                    "longest_recent_run_km", "weekly_volume_km_4wk_avg",
                    "max_session_minutes",
                )
            )
        )
        self.assertEqual([], baseline_b["strength_loads"])
        self.assertFalse(state_dir_b.exists())  # still only a preview

        status, applied_b_init = self.call(
            "POST",
            "/v1/coach/initialization/apply",
            body={
                "initialization_request": SECOND_ATHLETE_ONBOARDING,
                "proposal": prepared_b["proposal"],
                "confirmed": True,
            },
            token=TOKEN_B,
        )
        self.assertEqual(200, status, applied_b_init)
        self.assertEqual(1, applied_b_init["plan_version"])
        self.assertFalse(applied_b_init["idempotent_replay"])
        self.assertTrue(state_dir_b.exists())
        b_plan_id = applied_b_init["plan_id"]
        self.assertNotEqual(a_plan["plan_id"], b_plan_id)

        session_b_after_init = self.session_for(TOKEN_B)
        b_session = session_b_after_init["plan_state"]["current_plan"]["week"]["sessions"][0]
        b_session_id = b_session["session_id"]
        # The shared fake transport only pre-registers A's own plan's workout names for
        # read-back synthesis (see FakeIntervals.__init__ / _readback: an open-effort
        # session carries no workout_doc of its own the way an hr_ceiling one does, so the
        # fake reconstructs it from a name it already knows). Teach it B's new session's
        # name too, so B's own delivery can be verified the same way A's is.
        self.fake.steps_by_name[b_session["plan"]["name"]] = b_session["plan"]["steps"]

        # 5. From here the two athletes interleave call for call: A reviews Thursday's
        #    quality session, then B moves her only session -- each stepping only their
        #    own plan version, and neither response naming a thing that belongs to the
        #    other.
        applied_a = self.decide_for(TOKEN_A, WEEKLY_CHANGE)
        self.assertEqual(2, applied_a["plan_version"])

        applied_b = self.decide_for(
            TOKEN_B,
            {
                "summary": "把這堂課挪到週日",
                "reason_codes": ["schedule_or_equipment_changed"],
                "evidence": [{"field": "constraints", "observation": "週五臨時有事"}],
                "goal_effect": {"week": "同一堂課換一天", "cycle": "28 天方向不變"},
                "next_review_condition": "移動後重新確認交付",
                "sessions": [
                    {
                        "operation": "move",
                        "session_id": b_session_id,
                        "scheduled_date": "2026-08-16",
                    }
                ],
            },
        )
        self.assertEqual(2, applied_b["plan_version"])

        session_a_blob = json.dumps(self.session_for(TOKEN_A))
        session_b_blob = json.dumps(self.session_for(TOKEN_B))
        for marker in (
            b_plan_id,
            b_session_id,
            SECOND_ATHLETE_ONBOARDING["goal"]["outcome"],
            "挪到週日",
        ):
            self.assertNotIn(marker, session_a_blob)
        for marker in (
            a_plan["plan_id"],
            "run-quality-01",
            "strength-full-01",
            a_plan["goal"]["outcome"],
            WEEKLY_CHANGE["summary"],
        ):
            self.assertNotIn(marker, session_b_blob)

        # A's own store is done changing for the rest of this test. Read it directly off
        # disk (not through /v1/coach/session, which is free to reconcile fresh evidence
        # and would then be A's own call touching A's store) so everything from here on
        # can only be catching something one of B's calls did.
        a_plan_before = read_current_plan(state_dir_a)
        a_snapshot = self.snapshot(state_dir_a)

        # 6. B delivers her moved session over the same fake transport A uses. It is
        #    written under B's own bearer token.
        calls_before = len(self.fake.authorizations)
        published_b = self.deliver_for(TOKEN_B, [b_session_id])
        self.assertEqual("intervals_accepted", published_b["delivery_state"])
        self.assertEqual([], published_b["unresolved"])
        new_authorizations = self.fake.authorizations[calls_before:]
        self.assertTrue(new_authorizations)
        self.assertTrue(
            all(header == "Bearer " + TOKEN_B for header in new_authorizations)
        )

        # 7. A fresh conversation for B -- a new call to /v1/coach/session, exactly what
        #    starting over in a new chat does -- reads back exactly what the calls above
        #    just wrote. Version 3: the move (2) plus the delivery's own
        #    ``delivery_verified`` commit (3), the same two-step bump
        #    ``test_the_whole_loop_stays_consistent_from_evidence_to_calendar`` shows for A.
        reopened_b = self.session_for(TOKEN_B)
        self.assertEqual(3, reopened_b["plan_state"]["plan_version"])
        delivered_view = next(
            item
            for item in reopened_b["delivery"]["sessions"]
            if item["session_id"] == b_session_id
        )
        self.assertEqual("intervals_accepted", delivered_view["delivery_state"])
        self.assertIsNotNone(delivered_view["external_id"])

        # 8. And across every one of B's calls above -- her decision, her delivery, her
        #    fresh session -- A's own store never moved again: byte for byte, not merely
        #    field by field.
        self.assertEqual(a_plan_before, read_current_plan(state_dir_a))
        self.assertEqual(a_snapshot, self.snapshot(state_dir_a))
