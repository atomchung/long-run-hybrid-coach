from __future__ import annotations

import base64
import contextlib
import copy
import dataclasses
import datetime as dt
import errno
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
    OPENAI_APPS_CHALLENGE_ENV_VAR,
    OPENAI_APPS_CHALLENGE_PATH,
    RAILWAY_GIT_COMMIT_ENV_VAR,
    RELEASE_ARTIFACT_SHA_ENV_VAR,
    RELEASE_COMMIT_ENV_VAR,
    RELEASE_DOMAIN_ENV_VAR,
    RELEASE_ID_ENV_VAR,
    LEGACY_RELEASE_OPENAPI_SHA_ENV_VAR,
    RELEASE_INSTRUCTIONS_SHA_ENV_VAR,
    RELEASE_SKILL_SHA_ENV_VAR,
    RELEASE_TOOL_CATALOGUE_SHA_ENV_VAR,
    CoachGateway,
    PRODUCT_VERSION,
    CoachGatewayHandler,
    CoachGatewayServer,
    GatewayError,
    GatewayConfig,
    GatewayConfigError,
    _calendar_disagreements,
    _initialization_claims,
    gateway_artifact_sha256,
    load_config,
    run_gateway,
    run_preflight,
)
from garmin_coach_loop import athlete_evidence, orchestration, security_log, token_envelope
from garmin_coach_loop import store as store_module
from garmin_coach_loop.gateway import INTERVALS_OAUTH_SCOPES, MCP_PATH, ROUTES
from garmin_coach_loop.mcp_transport import tool_catalogue_sha256
from garmin_coach_loop.delivery import IntervalsTransport, hr_ceiling_percent_lthr
from garmin_coach_loop.source_intervals import IntervalsCredentials
from garmin_coach_loop.release_identity import make_deployment_identity, make_release_id
from garmin_coach_loop.identity import (
    lookup_or_create_owner,
    owner_for_fingerprint,
    record_token_fingerprint,
    IdentityError,
    activity_report,
    owner_entry_origins,
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
    MAINTENANCE_FENCE_SCHEMA_VERSION,
    WRITER_CONTRACT_VERSION,
    adopt_store,
    apply_decision,
    canonical_hash,
    default_state_dir,
    doctor_store,
    init_store,
    maintenance_fence_path,
    open_delivery_attempt,
    read_current_plan,
    resolve_state_dir,
    restore_snapshot,
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

# The example clients the OAuth tests register, admitted here the way a real deployment
# admits a hosted platform -- by configuration. A test about what a *registered* client
# may then do should not also be a test of who may register, and the tests that are about
# that (`McpRegistrationTrustTests`) build their own gateway with none of these.
TEST_CLIENT_ORIGINS = (
    "https://client.example",
    "https://client.example:8443",
    "https://other.example",
)


def load(name: str) -> dict[str, Any]:
    return json.loads((EXAMPLE / name).read_text(encoding="utf-8"))


def release_identity_for(commit: str) -> dict[str, str]:
    """One valid, self-binding release identity, told apart by the commit it names.

    The catalogue and package digests are the real ones so that a gateway configured with
    this also reports ready; every other field is synthetic, because what these tests need
    from a release is only that two of them differ.
    """
    identity = {
        "git_commit": commit,
        "instructions_sha256": "1" * 64,
        "tool_catalogue_sha256": tool_catalogue_sha256(),
        "skill_sha256": "2" * 64,
        "gateway_domain": "https://gateway.example",
        "gateway_artifact_sha256": gateway_artifact_sha256(),
    }
    identity["release_id"] = make_release_id(**identity)
    return identity


def publishable_plan() -> dict[str, Any]:
    """The fixture plan with its two running sessions marked deliverable."""
    plan = load("plan-state-v1.json")
    for session in plan["week"]["sessions"]:
        if session["session_id"] in {"run-quality-01", "run-long-01"}:
            session["execution"]["publish_supported"] = True
    return plan


def recovery_signals_upload(
    *,
    source: str = "personal-os:recovery_daily+daily_metrics",
) -> dict[str, Any]:
    """One generic, already-sanitized client upload for the fixture session window."""
    return {
        "source": source,
        # Intentionally oldest first: the boundary owns stable newest-first projection,
        # so a generic client does not have to rediscover a presentation detail.
        "days": [
            {
                "date": "2026-08-12",
                "readiness_score": 61.0,
                "readiness_level": "MODERATE",
                "hrv_status": "BALANCED",
                "hrv_7d_avg_ms": 72.0,
                "acute_load": 410.0,
                "recovery_time_sec": 3600.0,
                "body_battery_high": 83.0,
                "body_battery_low": 34.0,
                "avg_stress": 22.0,
            },
            {
                "date": "2026-08-13",
                "readiness_score": 48.0,
                "readiness_level": "LOW",
                "hrv_status": "UNBALANCED",
                "hrv_7d_avg_ms": 67.0,
                "acute_load": 455.0,
                "recovery_time_sec": 7200.0,
                "body_battery_high": 64.0,
                "body_battery_low": 21.0,
                "avg_stress": 31.0,
            },
        ],
    }


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
        # One capability refused while the rest of the connection works, which is the
        # 2026-08-18 token: `/events` answered 403 while `/sport-settings` answered 200.
        # Unlike `sport_settings` this defaults to open, because a calendar the product
        # cannot read is the exception a test asks for by name.
        self.calendar_status: int | None = None
        self.settings_write_status: int | None = None
        self.settings_updates: list[dict[str, Any]] = []
        # Default to a refusal so every optional-settings fallback remains covered. Tests
        # that need a readable settings response assign a list here.
        self.sport_settings: list[dict[str, Any]] | None = None
        self.steps_by_name: dict[str, list[dict[str, Any]]] = {}
        # Per-activity segment breakdowns, keyed by activity id. Empty by default,
        # because an activity the provider has not analyzed answers with no segments
        # and that is the shape most reads take. A test or scenario that wants the
        # per-segment read to actually return something assigns the rows here.
        self.segments_by_activity: dict[str, list[dict[str, Any]]] = {}
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
        if method == "PUT" and url.endswith("/sport-settings/Run"):
            if self.settings_write_status is not None:
                raise _http_error(url, self.settings_write_status)
            patch = json.loads((request.data or b"{}").decode("utf-8"))
            self.settings_updates.append(copy.deepcopy(patch))
            if self.sport_settings is None:
                raise _http_error(url, 403)
            run = next(
                (item for item in self.sport_settings if "Run" in (item.get("types") or [])),
                None,
            )
            if run is None:
                run = {"types": ["Run"]}
                self.sport_settings.append(run)
            run.update(patch)
            return json.dumps(run).encode("utf-8")
        if "/activities?" in url:
            return json.dumps(self.activities).encode("utf-8")
        if "/wellness?" in url:
            return json.dumps(self.wellness).encode("utf-8")
        if self.calendar_status is not None and "/events" in url:
            raise _http_error(url, self.calendar_status)
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
            # an error, which is the shape the reader is written against -- and stays
            # the default here. An activity this fake was taught a breakdown for
            # answers with it, so the per-segment read has something to read.
            activity_id = urllib.parse.urlsplit(url).path.split("/")[-2]
            return json.dumps(
                {"icu_intervals": self.segments_by_activity.get(activity_id, [])}
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
        if stored.get("external_id") in self.corrupt_external_ids:
            result["workout_doc"] = {"steps": []}
        return result


class _RecordingHandler(logging.Handler):
    """Every line the gateway wrote, with a way to wait for the one that ends a request.

    ``_dispatch`` writes its access line *after* the response body is on the wire, so a
    client that has already read the body can reach its assertions while the serving
    thread has not yet reached the log call. Reading ``records`` at that moment is a race
    against a thread, not a statement about the gateway, and it fails in whichever order
    the two happen to interleave. ``wait_for_last`` closes it at the source: the access
    line is the final write of a dispatch, so waiting for it means the serving thread is
    finished with this request and ``records`` is complete.

    ``emitted`` counts every line ever written and is deliberately not reset by
    ``records.clear()``: a test that clears the log still needs "one more line has
    arrived since I asked" to mean the current request rather than an identical earlier
    one.
    """

    def __init__(self):
        super().__init__()
        self._written = threading.Condition()
        self.records: list[str] = []
        self.emitted = 0
        self._last = ""

    def emit(self, record: logging.LogRecord) -> None:
        message = record.getMessage()
        with self._written:
            self.records.append(message)
            self._last = message
            self.emitted += 1
            self._written.notify_all()

    def wait_for_last(self, prefix: str, *, after: int, timeout: float = 5.0) -> None:
        """Block until a line starting with ``prefix`` is the most recent one written.

        A timeout returns rather than raising: the caller is a helper used by every
        request in the suite, including the few that reach the server outside
        ``_dispatch``, and turning "no line came" into an error here would report it
        against whichever assertion happened to follow.
        """
        with self._written:
            self._written.wait_for(
                lambda: self.emitted > after and self._last.startswith(prefix), timeout
            )


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
            trusted_client_origins=TEST_CLIENT_ORIGINS,
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

        # Both streams into one recorder, deliberately: every assertion that a credential
        # never reaches a log then covers the security events too, without any of them
        # having to know a second stream exists.
        self.log_handler = _RecordingHandler()
        self.loggers = [
            logging.getLogger(name)
            for name in ("garmin_coach_loop.gateway", security_log.LOGGER_NAME)
        ]
        self._previous_levels = [logger.level for logger in self.loggers]
        for logger in self.loggers:
            logger.setLevel(logging.DEBUG)
            logger.addHandler(self.log_handler)

    def tearDown(self):
        for logger, level in zip(self.loggers, self._previous_levels):
            logger.removeHandler(self.log_handler)
            logger.setLevel(level)
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
        headers: dict[str, str] | None = None,
    ) -> tuple[int, Any]:
        data = raw if raw is not None else (None if body is None else json.dumps(body).encode())
        request = urllib.request.Request(self.base_url + path, data=data, method=method)
        if data is not None:
            request.add_header("Content-Type", content_type)
        if token is not None:
            request.add_header("Authorization", "Bearer " + token)
        for name, value in (headers or {}).items():
            request.add_header(name, value)
        # Taken before the request, so "a line arrived after this" cannot be satisfied by
        # an identical line an earlier call left behind.
        before = self.log_handler.emitted
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                answer = response.status, json.loads(response.read() or b"{}")
        except urllib.error.HTTPError as exc:
            with exc:
                answer = exc.code, json.loads(exc.read() or b"{}")
        # The response is on the wire before the access line is written. Every assertion
        # about what was logged runs after this call returns, so it waits here once
        # rather than in each of them.
        self.log_handler.wait_for_last(
            "%s %s -> " % (method, urllib.parse.urlsplit(path).path), after=before
        )
        return answer

    def mcp_bearer(self, provider_token: str, *, base_url: str | None = None) -> str:
        """The access token this gateway's own token endpoint would have issued.

        Sealed here rather than danced for, so that a test about the transport is not
        also a test about OAuth. ``McpAuthorizationServerTests`` runs the real flow and
        proves this shortcut mints the same thing the endpoint does.

        It lives in the base class because ``/mcp`` is the only entry over a socket:
        every HTTP-layer assertion in this file -- method, size, media type, body drain,
        response headers, the access line -- is made against a request that has to carry
        one of these.
        """
        return token_envelope.seal(
            {
                "intervals_token": provider_token,
                "aud": (base_url or self.base_url) + "/mcp",
                "scope": ",".join(INTERVALS_OAUTH_SCOPES),
                "iat": int(self.now.timestamp()),
            },
            kind=token_envelope.ACCESS_TOKEN,
            key=HMAC_KEY,
        )

    def tool_rpc(self, name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
        """One JSON-RPC ``tools/call`` message, for the assertions that are about HTTP.

        The transport's own behaviour is proven in ``test_mcp_gateway``; what this is
        for is giving a size, media-type or access-log assertion a body the one entry
        actually accepts.
        """
        return {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments or {}},
        }

    def route(
        self,
        kind: str,
        *,
        body: Any = None,
        token: str | None,
    ) -> tuple[int, Any]:
        """One coaching act, dispatched the way an authenticated entry dispatches it.

        The counterpart to ``call``, and the split between them is deliberate. ``call``
        goes over a real socket and is therefore the only place an HTTP fact can be
        asserted; this one skips the socket entirely, so a test that is about what the
        coach *decides* is not also paying for a server round trip or asserting an
        answer no client would see. Everything an entry does before dispatch -- reading
        the bearer, resolving the owner, refusing an unknown one -- is proven at ``/mcp``
        rather than restated here.

        The status is reassembled from ``GatewayError`` so both helpers return the same
        ``(status, payload)`` pair, which is what lets a behaviour assertion read the
        same either side of the split.
        """
        gateway = self.gateway
        try:
            owner_id = gateway.resolve_owner(token)
            payload = gateway.route(kind, owner_id, str(token), body or {})
        except GatewayError as exc:
            return int(exc.status), exc.payload()
        return 200, payload

    def security_events(self) -> list[dict[str, Any]]:
        """Every security event written so far, parsed back out of the log line."""
        return [
            json.loads(message.split(" ", 1)[1])
            for message in self.log_handler.records
            if message.startswith("security {")
        ]

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


# The material one injected filesystem failure carries, one string per class of thing the
# boundary must never republish (issue #282). Written into the exception the way the
# operating system writes it: `str(OSError)` is "[Errno N] <strerror>: '<filename>'", so
# the filename argument carries the deployment path, the opaque owner id and a
# secret-looking filename, and the strerror carries a newline and a bearer-shaped string.
LEAKY_STATE_ROOT = "/srv/deploy-42/coach-state"
LEAKY_OWNER_ID = "9f1c0f6e-7a55-4a2b-9d3e-0c8b4e2f1a77"
LEAKY_SECRET_FILE = "intervals-refresh-token.key"
LEAKY_FILENAME = f"{LEAKY_STATE_ROOT}/owners/{LEAKY_OWNER_ID}/{LEAKY_SECRET_FILE}"
# Credential-shaped, but without the `Bearer ` scheme word in front of it: the
# repository safety scan reads that spelling as a real leaked token wherever it
# appears, including here. What this fixture needs is the *shape*.
LEAKY_STRERROR = "Permission denied\nwhile holding sk-live-4d9a0f31c2b84e77"
LEAKY_MATERIAL = (
    LEAKY_STATE_ROOT,
    LEAKY_OWNER_ID,
    LEAKY_SECRET_FILE,
    "sk-live-4d9a0f31c2b84e77",
)


def leaky_oserror(code: int = errno.EACCES) -> OSError:
    """One filesystem failure whose own text names everything a client must not see."""
    return OSError(code, LEAKY_STRERROR, LEAKY_FILENAME)


@contextlib.contextmanager
def unreadable(*names: str):
    """Make named store files fail the way a lost or unreadable volume makes them fail."""
    real = Path.read_text

    def read_text(self, *args, **kwargs):
        if self.name in names:
            raise leaky_oserror()
        return real(self, *args, **kwargs)

    with mock.patch.object(Path, "read_text", read_text):
        yield


@contextlib.contextmanager
def unwritable(*names: str):
    """Make the atomic replace that lands a named store file fail on the volume."""
    real = os.replace

    def replace(source, destination, *args, **kwargs):
        if Path(destination).name in names:
            raise leaky_oserror(errno.ENOSPC)
        return real(source, destination, *args, **kwargs)

    with mock.patch.object(store_module.os, "replace", replace):
        yield


class IntervalsCodeRedemptionTests(GatewayTestCase):
    """The one place a provider code becomes a provider token.

    Reached through `/oauth/callback` in the MCP flow; called here directly, because what
    these assert belongs to the redemption itself -- which athlete a token is filed under,
    what is written down, and what a failure is allowed to say -- not to any one route
    that reaches it.
    """

    def _redeem(self, code: str = "c1") -> dict[str, Any]:
        return self.gateway._redeem_intervals_code(code)

    def test_redeeming_a_code_registers_the_athlete_and_stores_no_token(self):
        self.fake.token_payload = {
            "token_type": "Bearer",
            "access_token": TOKEN_A,
            "scope": "ACTIVITY:READ",
            "athlete": {"id": "i1", "name": "Fixture Athlete"},
        }

        redeemed = self._redeem()

        self.assertEqual(TOKEN_A, redeemed["access_token"])
        self.assertEqual(("ACTIVITY:READ",), tuple(redeemed["scope_names"]))

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

    def test_redemption_normalizes_and_records_scope_names_only(self):
        self.fake.token_payload = {
            "access_token": TOKEN_A,
            "scope": "WELLNESS:READ, SETTINGS:WRITE ACTIVITY:READ ignored-value",
            "athlete": {"id": "i1", "name": "provider-name-must-not-persist"},
        }

        redeemed = self._redeem()

        expected = ("ACTIVITY:READ", "SETTINGS:WRITE", "WELLNESS:READ")
        self.assertEqual(expected, tuple(redeemed["scope_names"]))
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
        self._redeem("c1")
        first = owner_for_fingerprint(
            self.identity_db, token_fingerprint(TOKEN_A, hmac_key=HMAC_KEY)
        )
        self.assertIsNotNone(first)

        self.fake.token_payload = {
            "access_token": TOKEN_B,
            "scope": "ACTIVITY:READ",
            "athlete": {"id": "i1"},
        }
        self._redeem("c2")

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

    def test_upstream_failure_returns_a_generic_error_and_leaks_nothing(self):
        self.fake.token_status = 400

        with self.assertRaises(GatewayError) as raised:
            self._redeem()

        self.assertEqual(502, raised.exception.status)
        self.assertEqual("server_error", raised.exception.code)
        blob = str(raised.exception.detail) + " ".join(self.log_handler.records)
        for secret in (CLIENT_SECRET_VALUE, "c1", TOKEN_A):
            self.assertNotIn(secret, blob)
        self.assertFalse(self.identity_db.exists())

    def test_a_response_without_an_athlete_identity_is_refused(self):
        # No athlete id means no way to say which store the token may open.
        self.fake.token_payload = {"access_token": TOKEN_A, "scope": "ACTIVITY:READ"}

        with self.assertRaises(GatewayError) as raised:
            self._redeem()

        self.assertEqual(502, raised.exception.status)
        self.assertEqual("server_error", raised.exception.code)
        self.assertFalse(self.identity_db.exists())


# --------------------------------------------------------------------------------------
# Identity boundary
# --------------------------------------------------------------------------------------


class GatewayIdentityBoundaryTests(GatewayTestCase):
    def test_unknown_token_is_refused_before_any_provider_or_state_read(self):
        status, payload = self.route("session", body={}, token=UNKNOWN_TOKEN)

        self.assertEqual(401, status)
        self.assertEqual({"status": "blocked", "error": "unauthorized"}, payload)
        self.assertEqual([], self.fake.calls)
        self.assertFalse((self.state_root / "owners").exists())

    def test_missing_authorization_header_is_refused(self):
        # Over the socket, because "no header" is a fact about the request rather than
        # about a credential: `route` is only ever reached with one, so a helper call
        # here would be testing an unseeded token instead -- which is the case above.
        status, payload = self.call("POST", MCP_PATH, body=self.tool_rpc("startCoachSession"))
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
            status, payload = self.route("session", body={}, token=TOKEN_A)

        self.assertEqual(200, status)
        self.assertEqual("fixture-plan-001", payload["plan_state"]["plan_id"])
        self.assertEqual(
            "fixture-plan-002", read_current_plan(self.owner_dir(owner_b))["plan_id"]
        )

    def test_owner_without_a_store_gets_an_explicit_answer_and_no_store_is_created(self):
        owner_id = self.seed_owner(TOKEN_A)
        status, payload = self.route("session", body={}, token=TOKEN_A)

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
        status, payload = self.route("session", body={}, token=TOKEN_A)

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
        _, payload = self.route("session", body={}, token=TOKEN_A)
        states = {entry["delivery_state"] for entry in payload["delivery"]["sessions"]}
        self.assertEqual({"not_published"}, states)
        self.assertNotIn("garmin", json.dumps(payload["delivery"]).lower())

    def test_revoked_token_fails_explicitly_and_leaves_plan_state_untouched(self):
        before = self.snapshot(self.state_dir)
        self.fake.read_status = 401

        status, payload = self.route("session", body={}, token=TOKEN_A)

        self.assertEqual(502, status)
        self.assertEqual("provider_error", payload["error"])
        self.assertEqual("blocked", payload["status"])
        self.assertEqual(before, self.snapshot(self.state_dir))
        self.assertEqual(1, read_current_plan(self.state_dir)["current_version"])

    def test_malformed_session_input_is_a_request_error_not_a_provider_error(self):
        status, payload = self.route(
            "session", body={"timezone": "Nowhere/Nothing"}, token=TOKEN_A
        )
        self.assertEqual(400, status)
        self.assertEqual("invalid_request", payload["error"])
        self.assertEqual([], self.fake.calls)

    def test_client_uploaded_recovery_signals_reach_this_context_without_a_local_db_read(self):
        before = self.snapshot(self.state_dir)
        with mock.patch(
            "garmin_coach_loop.source_personal_os.fetch_recovery_signals",
            side_effect=AssertionError("hosted gateway must never read health.db"),
        ):
            status, payload = self.route(
                "session",
                body={"recovery_signals": recovery_signals_upload()},
                token=TOKEN_A,
            )

        self.assertEqual(200, status, payload)
        group = payload["context"]["recovery_signals"]
        self.assertEqual(
            "client-uploaded:personal-os:recovery_daily+daily_metrics", group["source"]
        )
        self.assertEqual(
            ["2026-08-13", "2026-08-12"], [day["date"] for day in group["days"]]
        )
        self.assertEqual("2026-08-07", group["window_start"])
        self.assertEqual("2026-08-13", group["window_end"])
        self.assertEqual(48.0, group["days"][0]["readiness_score"])
        self.assertFalse(
            [note for note in payload["unknowns"] if "no client upload supplied" in note]
        )
        # The group is context input, not a second owner store or a retained health copy.
        self.assertEqual(before, self.snapshot(self.state_dir))

        _, next_session = self.route(
            "session", body={}, token=TOKEN_A
        )
        self.assertIsNone(next_session["context"]["recovery_signals"])
        self.assertTrue(
            [
                note
                for note in next_session["unknowns"]
                if "no client upload supplied" in note
            ]
        )

    def test_empty_recovery_days_means_the_client_looked_and_found_no_values(self):
        group = recovery_signals_upload()
        group["days"] = []

        status, payload = self.route(
            "session",
            body={"recovery_signals": group},
            token=TOKEN_A,
        )

        self.assertEqual(200, status, payload)
        self.assertEqual([], payload["context"]["recovery_signals"]["days"])
        self.assertFalse(
            [
                note
                for note in payload["unknowns"]
                if "no client upload supplied" in note
            ]
        )

    def test_malformed_or_misaligned_recovery_upload_is_refused_before_provider_read(self):
        all_null = recovery_signals_upload()
        all_null["days"] = [
            {key: ("2026-08-13" if key == "date" else None)
             for key in all_null["days"][0]}
        ]
        duplicate = recovery_signals_upload()
        duplicate["days"] = [duplicate["days"][0], copy.deepcopy(duplicate["days"][0])]
        non_finite = recovery_signals_upload()
        non_finite["days"][0]["readiness_score"] = float("nan")
        unrepresentable_number = recovery_signals_upload()
        unrepresentable_number["days"][0]["acute_load"] = 10**400
        boolean_number = recovery_signals_upload()
        boolean_number["days"][0]["acute_load"] = True
        impossible_percentage = recovery_signals_upload()
        impossible_percentage["days"][0]["body_battery_high"] = 101.0
        too_many_days = recovery_signals_upload()
        too_many_days["days"] = [
            copy.deepcopy(too_many_days["days"][0]) for _ in range(8)
        ]
        cases = (
            {
                **recovery_signals_upload(),
                "days": [
                    {
                        **recovery_signals_upload()["days"][0],
                        "date": "2026-08-06",
                    }
                ],
            },
            duplicate,
            all_null,
            non_finite,
            unrepresentable_number,
            boolean_number,
            impossible_percentage,
            too_many_days,
            {**recovery_signals_upload(), "extra": "raw-provider-payload"},
            recovery_signals_upload(source="private/health.db"),
        )
        for group in cases:
            with self.subTest(group=group):
                self.fake.calls.clear()
                status, payload = self.route(
                    "session",
                    body={"recovery_signals": group},
                    token=TOKEN_A,
                )
                self.assertEqual(400, status, payload)
                self.assertEqual("invalid_request", payload["error"])
                self.assertEqual([], self.fake.calls)

    def test_a_day_carrying_one_reading_is_accepted_and_the_rest_read_as_null(self):
        """Issue #187: the client sends what it has, the gateway fills in the unknowns.

        This is the shape an athlete reading one number off a watch face produces. It
        used to cost nine explicit nulls, which is the opposite of what omission means
        anywhere else here. The stored CoachContext still carries every key, because the
        filling happens on the way in rather than being asked of whoever is typing.
        """
        group = recovery_signals_upload(source="athlete-reported")
        group["days"] = [{"date": "2026-08-13", "sleep_score": 78.0}]

        status, payload = self.route(
            "session",
            body={"recovery_signals": group},
            token=TOKEN_A,
        )

        self.assertEqual(200, status, payload)
        stored = payload["context"]["recovery_signals"]
        self.assertEqual("client-uploaded:athlete-reported", stored["source"])
        day = stored["days"][0]
        self.assertEqual("2026-08-13", day["date"])
        self.assertEqual(78.0, day["sleep_score"])
        observations = {
            "readiness_score",
            "readiness_level",
            "hrv_status",
            "hrv_7d_avg_ms",
            "acute_load",
            "recovery_time_sec",
            "body_battery_high",
            "body_battery_low",
            "avg_stress",
            "sleep_duration_sec",
            "sleep_history_score",
            "hrv_last_night_ms",
            "resting_hr_bpm",
        }
        self.assertEqual({"date", "sleep_score", *observations}, set(day))
        for field in observations:
            self.assertIsNone(day[field], field)

    def test_a_day_with_no_reading_at_all_is_refused_by_its_own_date(self):
        """Omitting everything is not the same as observing nothing.

        The refusal names the day so the client can fix that one row and send the group
        again, rather than being told the upload was wrong somewhere.
        """
        group = recovery_signals_upload()
        group["days"] = [{"date": "2026-08-13"}]

        status, payload = self.route(
            "session",
            body={"recovery_signals": group},
            token=TOKEN_A,
        )

        self.assertEqual(400, status, payload)
        self.assertEqual("invalid_request", payload["error"])
        self.assertIn("2026-08-13", payload["detail"])
        self.assertEqual([], self.fake.calls)

    def test_the_five_later_readings_carry_their_own_domains(self):
        """Sleep, resting heart rate and one night's HRV, each refused by name and day.

        The bounds are the metric's physical domain, not a coaching threshold: nothing
        here decides whether a reading is good, and nothing compares it to another.
        """
        out_of_domain = {
            "sleep_score": 101.0,
            "sleep_duration_sec": 86401.0,
            "sleep_history_score": -1.0,
            # Zero milliseconds of heart-rate variability is a data source's "nothing
            # recorded" sentinel, never a measurement -- the same call hrv_7d_avg_ms makes.
            "hrv_last_night_ms": 0.0,
            "resting_hr_bpm": 19.0,
        }
        for field, value in out_of_domain.items():
            with self.subTest(field=field):
                self.fake.calls.clear()
                group = recovery_signals_upload()
                group["days"] = [{"date": "2026-08-13", field: value}]

                status, payload = self.route(
                    "session",
                    body={"recovery_signals": group},
                    token=TOKEN_A,
                )

                self.assertEqual(400, status, payload)
                self.assertEqual("invalid_request", payload["error"])
                self.assertIn(field, payload["detail"])
                self.assertIn("2026-08-13", payload["detail"])
                self.assertEqual([], self.fake.calls)

    def test_a_reading_the_athlete_read_out_is_as_ordinary_as_one_a_client_read(self):
        """The route in is not asked about; the declared source is recorded as stated.

        A watch face read aloud, an app screen, a CSV export: all of them are values plus
        a provenance label. What stays refused is a path, a credential, or a number no
        device produced -- none of which any of these labels is.
        """
        for label in ("athlete-reported", "garmin-connect-app", "csv-export"):
            with self.subTest(source=label):
                group = recovery_signals_upload(source=label)
                group["days"] = [
                    {"date": "2026-08-13", "resting_hr_bpm": 47.0, "sleep_score": 81.0}
                ]

                status, payload = self.route(
                    "session",
                    body={"recovery_signals": group},
                    token=TOKEN_A,
                )

                self.assertEqual(200, status, payload)
                stored = payload["context"]["recovery_signals"]
                self.assertEqual(f"client-uploaded:{label}", stored["source"])
                self.assertEqual(47.0, stored["days"][0]["resting_hr_bpm"])

    def test_every_session_response_carries_the_training_judgment_itself(self):
        """The coaching layer arrives with the answer instead of waiting to be fetched.

        Serving it as an MCP prompt did not deliver it: prompts are user-controlled by
        specification, so the model about to coach never saw the text. This field is the
        one channel that does not depend on a client choosing to fetch something, and the
        value is the same file, verbatim -- not a summary of it.
        """
        status, payload = self.route(
            "session", body={}, token=TOKEN_A
        )

        self.assertEqual(200, status, payload)
        self.assertEqual(orchestration.training_judgment(), payload["coaching_guidance"])
        self.assertIn("Hybrid running and strength judgment", payload["coaching_guidance"])
        # The two served texts stay distinct: this is the coaching layer, not sequencing.
        self.assertNotEqual(orchestration.instructions(), payload["coaching_guidance"])

    def test_observed_zeroes_survive_the_upload_boundary(self):
        group = recovery_signals_upload()
        group["days"] = [
            {
                **group["days"][0],
                "readiness_score": 0.0,
                "hrv_7d_avg_ms": None,
                "acute_load": 0.0,
                "recovery_time_sec": 0.0,
                "body_battery_high": 0.0,
                "body_battery_low": 0.0,
                "avg_stress": 0.0,
            }
        ]

        status, payload = self.route(
            "session",
            body={"recovery_signals": group},
            token=TOKEN_A,
        )

        self.assertEqual(200, status, payload)
        day = payload["context"]["recovery_signals"]["days"][0]
        for field in (
            "readiness_score",
            "acute_load",
            "recovery_time_sec",
            "body_battery_high",
            "body_battery_low",
            "avg_stress",
        ):
            self.assertEqual(0.0, day[field], field)

    def test_taipei_and_utc_resolve_different_as_of_dates_at_the_same_instant(self):
        # 2026-08-13T18:00:00Z is already 2026-08-14 in Taipei (UTC+8) but still
        # 2026-08-13 in UTC (issue #112): startCoachSession must answer from the
        # athlete's own requested timezone, never from the gateway host's clock or a
        # single hard-coded zone -- the same boundary proven directly against
        # build_window and status_store in tests/test_context_builder.py and
        # tests/test_state_store.py, now proven at the hosted entry point itself.
        self.now = dt.datetime(2026, 8, 13, 18, 0, tzinfo=dt.timezone.utc)

        _, taipei = self.route(
            "session", body={"timezone": "Asia/Taipei"}, token=TOKEN_A
        )
        _, utc = self.route("session", body={"timezone": "UTC"}, token=TOKEN_A)

        self.assertEqual("2026-08-14", taipei["context"]["as_of"][:10])
        self.assertEqual("2026-08-13", utc["context"]["as_of"][:10])

    def test_omitted_timezone_keeps_the_documented_asia_taipei_default(self):
        self.now = dt.datetime(2026, 8, 13, 18, 0, tzinfo=dt.timezone.utc)

        _, default = self.route("session", body={}, token=TOKEN_A)
        _, explicit = self.route(
            "session", body={"timezone": "Asia/Taipei"}, token=TOKEN_A
        )

        self.assertEqual(default["context"]["as_of"], explicit["context"]["as_of"])


# --------------------------------------------------------------------------------------
# State -- the genuinely read-only alternative to the session route
# --------------------------------------------------------------------------------------


class GatewayUsageCounterTests(GatewayTestCase):
    """The operator's usage counter, seen from the entry that actually increments it."""

    def setUp(self):
        super().setUp()
        self.owner_id = self.seed_owner(TOKEN_A, plan=publishable_plan())

    def test_an_authenticated_call_is_counted_under_the_tool_it_reached(self):
        for _ in range(2):
            status, _ = self.route("state", token=TOKEN_A)
            self.assertEqual(200, status)

        report = activity_report(self.identity_db)
        self.assertEqual(1, report["registered"])
        self.assertEqual(1, report["active"])
        entry = report["owners"][0]
        self.assertEqual(1, entry["active_days"])
        self.assertEqual({"state": 2}, entry["tools"])

    def test_an_unauthenticated_call_is_counted_against_nobody(self):
        status, _ = self.route("state", token="not-a-token")

        self.assertEqual(401, status)
        self.assertEqual(0, activity_report(self.identity_db)["active"])

    def test_a_counter_that_cannot_be_written_does_not_fail_the_coaching_call(self):
        """The whole reason this is swallowed: no reading of a statistic is worth a 500."""
        with mock.patch(
            "garmin_coach_loop.gateway.record_activity",
            side_effect=IdentityError("registry is locked"),
        ):
            status, payload = self.route("state", token=TOKEN_A)

        self.assertEqual(200, status, payload)
        self.assertEqual("passed", payload["status"])
        self.assertEqual(0, activity_report(self.identity_db)["active"])
        self.assertTrue(
            any("usage counter not recorded" in line for line in self.log_handler.records),
            self.log_handler.records,
        )

    def test_an_answered_call_and_a_refused_one_are_told_apart(self):
        """Issue #275: zero commits reads the same for three causes until this splits them."""
        status, _ = self.route("state", token=TOKEN_A)
        self.assertEqual(200, status)
        status, _ = self.route("decision_apply", token=TOKEN_A, body={})
        self.assertNotEqual(200, status)

        entry = activity_report(self.identity_db)["owners"][0]
        self.assertEqual(1, entry["accepted"])
        self.assertEqual({"plan_state_exists": 1}, entry["refused"])

    def test_a_refusal_is_filed_under_this_gateway_s_own_code_never_the_message(self):
        status, payload = self.route("decision_apply", token=TOKEN_A, body={})

        self.assertNotEqual(200, status)
        refused = activity_report(self.identity_db)["owners"][0]["refused"]
        self.assertEqual(["plan_state_exists"], list(refused))
        rendered = repr(activity_report(self.identity_db))
        self.assertNotIn(payload.get("detail", "\u0000never"), rendered)

    def test_an_outcome_that_cannot_be_written_does_not_fail_the_coaching_call(self):
        with mock.patch(
            "garmin_coach_loop.gateway.record_call_outcome",
            side_effect=IdentityError("registry is locked"),
        ):
            status, payload = self.route("state", token=TOKEN_A)

        self.assertEqual(200, status, payload)
        self.assertEqual(0, activity_report(self.identity_db)["owners"][0]["accepted"])
        self.assertTrue(
            any("call outcome not recorded" in line for line in self.log_handler.records),
            self.log_handler.records,
        )

    def test_the_entry_an_athlete_arrived_through_is_narrowed_to_the_trust_list(self):
        """Issue #209: a platform this gateway already accepts, never a referrer or a port."""
        self.gateway._record_entry(self.owner_id, "https://claude.ai/api/mcp/auth_callback")
        self.gateway._record_entry(self.owner_id, "http://127.0.0.1:53219/callback")
        self.gateway._record_entry(self.owner_id, "https://connect.smithery.ai/callback")

        self.assertEqual(
            ["https://claude.ai", "local"],
            owner_entry_origins(self.identity_db, self.owner_id),
        )

    def test_an_entry_that_cannot_be_written_does_not_fail_the_connection(self):
        with mock.patch(
            "garmin_coach_loop.gateway.record_entry_origin",
            side_effect=IdentityError("registry is locked"),
        ):
            self.gateway._record_entry(self.owner_id, "https://claude.ai/callback")

        self.assertEqual([], owner_entry_origins(self.identity_db, self.owner_id))
        self.assertTrue(
            any("entry origin not recorded" in line for line in self.log_handler.records),
            self.log_handler.records,
        )

    def test_the_counter_never_writes_a_token_or_an_athlete_id_into_the_registry(self):
        self.route("state", token=TOKEN_A)

        blob = self.identity_db.read_bytes()
        self.assertNotIn(TOKEN_A.encode("utf-8"), blob)
        self.assertIn(b"state", blob)


class GatewayStateTests(GatewayTestCase):
    def setUp(self):
        super().setUp()
        self.owner_id = self.seed_owner(TOKEN_A, plan=publishable_plan())
        self.state_dir = self.owner_dir(self.owner_id)

    def test_reads_the_stored_plan_summary_with_no_provider_call(self):
        status, payload = self.route("state", token=TOKEN_A)

        self.assertEqual(200, status, payload)
        self.assertEqual("passed", payload["status"])
        self.assertEqual("fixture-plan-001", payload["plan_id"])
        self.assertEqual(1, payload["plan_version"])
        self.assertEqual("2026-08-10", payload["cycle"]["start"])
        self.assertEqual("2026-09-06", payload["cycle"]["end"])
        self.assertEqual(3, payload["cycle"]["outlook_weeks"])
        self.assertEqual("2026-08-10", payload["week"]["start"])
        self.assertEqual(7, payload["week"]["session_count"])
        self.assertEqual(
            publishable_plan()["goal"]["outcome"], payload["goal"]["outcome"]
        )
        self.assertEqual("intervals_accepted", payload["delivery"]["max_delivery_state"])
        self.assertIsNone(payload["pending_delivery_attempt_id"])
        self.assertTrue(payload["unknowns"])

        # The whole point: no request ever reached the injected provider.
        self.assertEqual([], self.fake.calls)

    def test_leaves_the_store_byte_for_byte_unchanged(self):
        before = self.snapshot(self.state_dir)

        status, _ = self.route("state", token=TOKEN_A)

        self.assertEqual(200, status)
        self.assertEqual(before, self.snapshot(self.state_dir))

    def test_an_open_delivery_reservation_surfaces_its_id_and_nothing_else_changes(self):
        attempt = open_delivery_attempt(
            self.state_dir,
            kind="delivery",
            plan_id="fixture-plan-001",
            plan_version=1,
            proposal_hash="deadbeef",
            operations=[
                {
                    "session_id": "run-long-01",
                    "operation": "upsert",
                    "owned_external_id": "gcl:test:owned",
                    "scheduled_date": "2026-08-13",
                }
            ],
        )

        _, payload = self.route("state", token=TOKEN_A)

        self.assertEqual(attempt["attempt_id"], payload["pending_delivery_attempt_id"])

    def test_an_account_with_no_plan_answers_explicitly_and_reaches_no_provider(self):
        owner_id = self.seed_owner(TOKEN_B, athlete_id="i2")

        status, payload = self.route("state", token=TOKEN_B)

        self.assertEqual(200, status)
        self.assertEqual("no_plan_state", payload["status"])
        self.assertIsNone(payload["plan_id"])
        self.assertIsNone(payload["plan_version"])
        self.assertIsNone(payload["cycle"])
        self.assertIsNone(payload["week"])
        self.assertIsNone(payload["delivery"])
        self.assertIsNone(payload["pending_delivery_attempt_id"])
        self.assertIn("no PlanState exists for this account", payload["unknowns"])
        self.assertEqual([], self.fake.calls)
        # Reading an account with no plan must not be the thing that creates one.
        self.assertFalse(self.owner_dir(owner_id).exists())

    def test_a_request_body_is_refused_rather_than_silently_ignored(self):
        status, payload = self.route(
            "state", body={"unexpected": True}, token=TOKEN_A
        )
        self.assertEqual(400, status)
        self.assertEqual("invalid_request", payload["error"])


# --------------------------------------------------------------------------------------
# Bootstrap -- the two paths that turn an authenticated identity into a readable store
# --------------------------------------------------------------------------------------


class GatewayPermissionDiagnosticTests(GatewayTestCase):
    def _exchange_connected_token(self) -> None:
        self.fake.token_payload = {
            "access_token": TOKEN_A,
            "scope": "CALENDAR:WRITE,ACTIVITY:READ,WELLNESS:READ,SETTINGS:WRITE",
            "athlete": {"id": "i1"},
        }
        self.gateway._redeem_intervals_code("fixture-code")

    def _assert_redacted_diagnostic_log(self) -> None:
        """Nothing this diagnostic reads about the athlete reaches a log line.

        The status it produced is asserted at the call site; how the request was
        classified in the access line is an HTTP fact, held over `/mcp` by
        `GatewayHttpSurfaceTests`.
        """
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

    def test_both_probes_report_readable_without_returning_provider_payload(self):
        self._exchange_connected_token()
        self.fake.sport_settings = [{"id": "provider-settings-must-not-escape"}]
        self.fake.events = [
            {
                "id": "77",
                "name": "calendar-content-must-not-escape",
                "start_date_local": self.now.date().isoformat() + "T06:00:00",
            }
        ]
        self.log_handler.records.clear()

        status, payload = self.route("permissions", token=TOKEN_A)

        self.assertEqual(200, status)
        self.assertEqual("passed", payload["status"])
        self.assertEqual("readable", payload["settings_read"])
        self.assertEqual("readable", payload["calendar_read"])
        self.assertEqual(
            ["ACTIVITY:READ", "CALENDAR:WRITE", "SETTINGS:WRITE", "WELLNESS:READ"],
            payload["scopes_recorded_at_authorization"],
        )
        rendered = json.dumps(payload)
        for forbidden in (
            TOKEN_A,
            "i1",
            "provider-settings-must-not-escape",
            "calendar-content-must-not-escape",
        ):
            self.assertNotIn(forbidden, rendered)
        self.assertEqual("Bearer " + TOKEN_A, self.fake.authorizations[-1])
        probed = [url for _, url in self.fake.calls]
        self.assertTrue(any(url.endswith("/athlete/0/sport-settings") for url in probed))
        self.assertTrue(any("/athlete/0/events?" in url for url in probed))
        self._assert_redacted_diagnostic_log()

    def test_both_probes_report_scope_denied_for_403(self):
        self._exchange_connected_token()
        self.fake.calendar_status = 403
        self.log_handler.records.clear()

        status, payload = self.route("permissions", token=TOKEN_A)

        self.assertEqual(200, status)
        self.assertEqual("denied", payload["settings_read"])
        self.assertEqual("denied", payload["calendar_read"])
        self._assert_redacted_diagnostic_log()

    def test_both_probes_report_invalid_or_expired_for_401(self):
        self._exchange_connected_token()
        self.fake.read_status = 401
        self.log_handler.records.clear()

        status, payload = self.route("permissions", token=TOKEN_A)

        self.assertEqual(200, status)
        self.assertEqual("invalid_or_expired", payload["settings_read"])
        self.assertEqual("invalid_or_expired", payload["calendar_read"])
        self._assert_redacted_diagnostic_log()

    def test_a_calendar_denied_to_a_token_recorded_with_calendar_write_is_reported(self):
        """The 2026-08-18 connection, which this diagnostic called healthy (issue #162).

        Its recorded scope list held `CALENDAR:WRITE` and its Settings read answered 200,
        so every check the product had said the connection was fine -- while the calendar
        read that a publish depends on answered 403, and the first hosted delivery failed
        with nothing but an HTTP code. The recorded list is still reported, because where
        a scope came from is worth knowing; what it is no longer allowed to do is stand
        in for the answer.
        """
        self._exchange_connected_token()
        self.fake.sport_settings = [{"id": "provider-settings-must-not-escape"}]
        self.fake.calendar_status = 403

        status, payload = self.route("permissions", token=TOKEN_A)

        self.assertEqual(200, status)
        self.assertEqual("readable", payload["settings_read"])
        self.assertEqual("denied", payload["calendar_read"])
        self.assertIn("CALENDAR:WRITE", payload["scopes_recorded_at_authorization"])

    def test_the_calendar_probe_asks_exactly_what_a_delivery_asks(self):
        """A diagnostic that reads something else is a second opinion about a third thing.

        `readable` here is only worth reading if it means the read a publish performs
        succeeds, so the probe is pinned to the delivery transport's own list call --
        same path, same query, same athlete -- rather than to a request built beside it.
        """
        self._exchange_connected_token()
        self.fake.calls.clear()

        status, _ = self.route("permissions", token=TOKEN_A)
        self.assertEqual(200, status)
        probed = [url for method, url in self.fake.calls if method == "GET" and "/events" in url]

        delivered: list[str] = []

        def record(request: urllib.request.Request) -> bytes:
            delivered.append(request.full_url)
            return b"[]"

        IntervalsTransport(
            IntervalsCredentials(TOKEN_A, "0", "bearer"), fetch=record
        ).list_events(self.now.date().isoformat())

        self.assertEqual(delivered, probed)

    def test_unknown_token_is_refused_before_the_probe(self):
        status, payload = self.route("permissions", token=UNKNOWN_TOKEN)

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
        before_scope_rows = self.identity_db.read_bytes()

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

                status, payload = self.route("permissions", token=TOKEN_A)

                self.assertEqual(200, status)
                self.assertEqual("passed", payload["status"])
                self.assertIsNone(payload["scopes_recorded_at_authorization"])
                self.assertEqual(expected, payload["settings_read"])
                self.assertEqual(expected, payload["calendar_read"])
                self.assertEqual(
                    {"GET"}, {method for method, _ in self.fake.calls}, self.fake.calls
                )
                rendered = json.dumps(payload) + "\n".join(self.log_handler.records)
                for forbidden in (
                    TOKEN_A,
                    fingerprint,
                    legacy_owner,
                    legacy_athlete,
                    "provider-settings-must-not-escape",
                ):
                    self.assertNotIn(forbidden, rendered)

        # The registry is no longer byte-identical after a diagnostic, and deliberately so:
        # every authenticated call increments this owner's usage counter, which is a write.
        # What must still hold is the thing that assertion was standing in for -- a legacy
        # connection's scopes stay unknown rather than being invented to fill the new table.
        self.assertNotEqual(before_scope_rows, self.identity_db.read_bytes())
        self.assertIsNone(scopes_for_fingerprint(self.identity_db, fingerprint))
        with sqlite3.connect(self.identity_db) as connection:
            recorded_scopes = connection.execute("SELECT COUNT(*) FROM token_scopes").fetchone()[0]
            counted = connection.execute(
                "SELECT SUM(calls) FROM activity_days WHERE owner_id = ?", (legacy_owner,)
            ).fetchone()[0]
        self.assertEqual(0, recorded_scopes)
        self.assertEqual(3, counted)

    def _assert_invalid_scope_object_fails_closed(self, replacement_ddl: str) -> None:
        self.seed_owner(TOKEN_A)
        fingerprint = token_fingerprint(TOKEN_A, hmac_key=HMAC_KEY)
        with sqlite3.connect(self.identity_db) as connection:
            connection.execute("DROP TABLE token_scopes")
            connection.execute(replacement_ddl)
        self.log_handler.records.clear()

        status, payload = self.route("permissions", token=TOKEN_A)

        self.assertEqual(500, status)
        self.assertEqual({"status": "blocked", "error": "internal_error"}, payload)
        self.assertEqual([], self.fake.calls)
        rendered = json.dumps(payload) + "\n".join(self.log_handler.records)
        self.assertNotIn(TOKEN_A, rendered)
        self.assertNotIn(fingerprint, rendered)

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
        "outlook": [
            {
                "week_start": "2026-08-24",
                "intent": "先把量拉起來，強度不動",
                "key_sessions": ["一次品質跑", "一次長的輕鬆跑", "兩次重訓"],
                "relation_to_primary": "推進主要適應",
            },
            {
                "week_start": "2026-08-31",
                "intent": "維持同樣的形狀，讓身體吸收",
                "key_sessions": ["一次品質跑", "一次長的輕鬆跑", "兩次重訓"],
                "relation_to_primary": "維持主要適應",
            },
            {
                "week_start": "2026-09-07",
                "intent": "量降下來，做這個週期自己的測量",
                "key_sessions": ["一次品質跑", "一次長的輕鬆跑", "兩次重訓"],
                "relation_to_primary": "量測主要適應",
            },
        ],
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


def rest_day(**overrides: Any) -> dict[str, Any]:
    """A day the first plan asks the athlete to do nothing on."""
    session = {
        "sport": "rest",
        "scheduled_date": "2026-08-13",
        "purpose": "回報症狀，今天不安排訓練",
        "adaptation": "recovery",
        "body_stress": "systemic",
        "cost": "easy",
        "priority": "flexible",
        "planned_minutes": 0,
        "fallback": {"action": "rest", "description": "維持休息"},
        "plan": {"kind": "unstructured"},
    }
    session.update(overrides)
    return session


def onboarding_starting_today(*sessions: dict[str, Any]) -> dict[str, Any]:
    """The same onboarding conversation, for an athlete whose block starts today.

    ``ONBOARDING`` opens four days out, so every question about *today* is answered
    before it is asked. An athlete who wants to start now is the ordinary case and the
    only one where what they just said about today can collide with the first week.
    """
    request = onboarding_sessions(*(sessions or (easy_run(scheduled_date="2026-08-13"),)))
    request["cycle"] = {
        **request["cycle"],
        "start": "2026-08-13",
        "outlook": [
            {**week, "week_start": start}
            for week, start in zip(
                request["cycle"]["outlook"], ("2026-08-20", "2026-08-27", "2026-09-03")
            )
        ],
    }
    return request


def as_change_request(request: dict[str, Any]) -> dict[str, Any]:
    """The same first plan, written the way the one plan-authoring contract takes it.

    Every field maps by name except the two the shapes spell differently and the
    sessions, which gain the operation verb a first plan's all share. Tests keep stating
    onboarding the way an onboarding conversation produces it; this is the only place
    that knows the wire shape, so a change to it fails here rather than in forty bodies.
    """
    change = {
        key: value
        for key, value in request.items()
        if key not in ("sessions", "week_intent", "baselines")
    }
    change["sessions"] = [
        {**session, "operation": "add"} for session in request["sessions"]
    ]
    change["week"] = {"intent": request["week_intent"]}
    if "baselines" in request:
        change["athlete_baseline"] = request["baselines"]
    return change


class GatewayInitializationTests(GatewayTestCase):
    """A first plan authored the way a model has to author it.

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
        return self.route(
            "decision_prepare",
            body={
                "change_request": as_change_request(
                    ONBOARDING if request is None else request
                )
            },
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
            "change_request": as_change_request(
                ONBOARDING if request is None else request
            ),
            "proposal": proposal,
        }
        if confirmed is not None:
            body["confirmed"] = confirmed
        return self.route("decision_apply", body=body, token=token)

    def initialization_proposal(
        self, owner_id: str, request: dict[str, Any], *, now: dt.datetime | None = None
    ) -> str:
        """The proposal prepare would have issued, for cases prepare itself refuses.

        Signed through the gateway's own issuer rather than through ``issue_proposal``
        directly, so it carries every stamp a real proposal carries -- the lifetime and
        the build that issued it. A hand-built claim set is one this gateway would refuse
        for the reason it refuses any proposal from an earlier build, which is a fact
        about the fixture rather than about the case under test.
        """
        moment = self.now if now is None else now
        plan = project_initialization_request(request, issued_at=moment)["plan"]
        return self.gateway._issue_proposal(
            _initialization_claims(owner=binding(owner_id, key=HMAC_KEY), initial_plan=plan),
            now=moment,
        )["proposal"]

    # -- the loop ---------------------------------------------------------------------

    def test_an_empty_account_becomes_a_readable_plan_through_one_confirmation(self):
        status, session = self.route("session", body={}, token=TOKEN_A)
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
        status, session = self.route("session", body={}, token=TOKEN_A)
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
        self.route("profile_record", body={"timezone": "Europe/Berlin"}, token=TOKEN_A)

        _, prepared = self.prepare()

        self.assertEqual("Europe/Berlin", prepared["athlete_profile"]["timezone"])
        self.assertEqual(
            [], [item for item in prepared["unknowns"] if "athlete_profile" in item]
        )

    def test_stating_a_profile_first_does_not_make_the_account_look_used(self):
        # The same guarantee availability has: an athlete may answer "where are you"
        # before there is anything to train, and initialization still runs.
        self.route("profile_record", body={"timezone": "Europe/Berlin"}, token=TOKEN_A)

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
        _, session = self.route("session", body={}, token=TOKEN_A)
        constraints = session["context"]["constraints"]
        # The next conversation opens knowing the days rather than asking for them again.
        self.assertEqual(["mon", "wed", "sat"], constraints["available_days"])
        self.assertEqual("athlete_evidence", constraints["availability_source"])

    def test_availability_stated_before_the_plan_does_not_block_creating_one(self):
        # The ordinary first conversation: the athlete answers "which days can you train"
        # in the first message, and the plan is decided several messages later.
        status, _ = self.route(
            "availability_record",
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
        _, session = self.route("session", body={}, token=TOKEN_A)
        self.assertEqual(
            ["mon", "wed", "sat"], session["context"]["constraints"]["available_days"]
        )

    # A filesystem failure on this path is a warning rather than a refusal: the plan is
    # committed before the days are stored and is never unwound. The warning is therefore
    # the one model-facing sentence a volume failure reaches directly, and it used to
    # interpolate the exception -- state root and owner id included (issue #282).

    def assertNothingLeaked(self, answer: Any) -> None:
        rendered = json.dumps(answer, ensure_ascii=False)
        for material in LEAKY_MATERIAL:
            self.assertNotIn(material, rendered)
        self.assertNotIn("Errno", rendered)

    def test_a_volume_that_cannot_take_the_days_still_leaves_the_plan_standing(self):
        request = onboarding(availability={"days": ["mon", "wed", "sat"]})
        _, prepared = self.prepare(request)

        with unwritable("athlete-evidence.json"):
            status, applied = self.initialize(prepared["proposal"], request=request)

        self.assertEqual(200, status, applied)
        self.assertEqual(1, applied["plan_version"])
        self.assertEqual(
            [
                "available days were not stored and will be asked again: "
                "cannot write athlete-evidence.json: the state volume is out of space"
            ],
            applied["warnings"],
        )
        self.assertNothingLeaked(applied)
        self.assertEqual(1, read_current_plan(self.state_dir)["current_version"])

    def test_a_failure_the_store_does_not_own_is_reduced_to_the_fact_it_reports(self):
        # `record_availability` creates the owner directory before it writes. That
        # `mkdir` is not one of the store's own read/write helpers, so its `OSError`
        # arrives here raw -- which is exactly the branch that used to be interpolated.
        request = onboarding(availability={"days": ["mon", "wed", "sat"]})
        _, prepared = self.prepare(request)
        real_mkdir = Path.mkdir

        def mkdir(self, *args, **kwargs):
            if self.name == LEAKY_OWNER_ID or self.is_dir():
                raise leaky_oserror()
            return real_mkdir(self, *args, **kwargs)

        with mock.patch.object(Path, "mkdir", mkdir):
            status, applied = self.initialize(prepared["proposal"], request=request)

        self.assertEqual(200, status, applied)
        self.assertEqual(1, applied["plan_version"])
        self.assertEqual(
            ["available days were not stored and will be asked again"], applied["warnings"]
        )
        self.assertNothingLeaked(applied)
        # The cause is not thrown away -- it goes to the operator log, which is where an
        # unhandled failure already leaves it, and not to the security log.
        self.assertIn("initial availability was not stored", "\n".join(self.log_handler.records))
        for event in self.security_events():
            self.assertNotIn(LEAKY_STATE_ROOT, json.dumps(event, ensure_ascii=False))

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

    def test_a_structured_hr_ceiling_without_any_anchor_is_refused(self):
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
            "without a measured athlete_baseline.max_hr or a stated easy_hr_ceiling anchor",
            " ".join(payload["validation"]["errors"]),
        )

    def test_a_structured_hr_ceiling_within_a_stated_easy_hr_ceiling_passes(self):
        """The false-positive control: an athlete-stated easy_hr_ceiling anchors it too."""
        request = onboarding_sessions(
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
        request["baselines"] = {**request["baselines"], "easy_hr_ceiling": 150}

        status, prepared = self.prepare(request)

        self.assertEqual(200, status)
        self.assertEqual("passed", prepared["status"])
        self.assertEqual(150, prepared["preview"]["athlete_baseline"]["easy_hr_ceiling"])

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

    def test_a_first_plan_prepared_by_another_build_creates_nothing(self):
        """The third route through the shared proposal check, and the one that starts a store.

        A first plan is re-derived at apply, so the projection that computed the preview
        has to be the projection that commits it. A build change between the two would
        otherwise create this athlete's plan from code they were never shown, and the
        account it creates is the one every later version descends from.
        """
        self.gateway.config = dataclasses.replace(
            self.config, release_identity=release_identity_for("a" * 40)
        )
        _, prepared = self.prepare()
        self.gateway.config = dataclasses.replace(
            self.config, release_identity=release_identity_for("b" * 40)
        )

        status, payload = self.initialize(prepared["proposal"])

        self.assertEqual(409, status, payload)
        self.assertEqual("proposal_mismatch", payload["error"])
        self.assertIn("different build of this gateway", payload["detail"])
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
        _, session = self.route("session", body={}, token=TOKEN_A)
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
        status, prepared = self.route("decision_prepare", body=body, token=TOKEN_A)
        self.assertEqual(200, status, prepared)
        status, applied = self.route(
            "decision_apply",
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

    # -- one plan-authoring contract, read as a first plan ---------------------------
    #
    # The translation the gateway does between the two projections. Each of these is a
    # refusal rather than a quiet correction: a first plan the model believes it sent and
    # a first plan the store received have to be the same plan.

    def change_request(self, **overrides: Any) -> dict[str, Any]:
        return {**as_change_request(ONBOARDING), **overrides}

    def prepare_raw(self, body: dict[str, Any], *, token: str | None = TOKEN_A):
        return self.route("decision_prepare", body=body, token=token)

    def test_a_first_plan_may_only_add_sessions(self):
        for operation in ("keep", "move", "reduce", "replace"):
            with self.subTest(operation=operation):
                sessions = copy.deepcopy(self.change_request()["sessions"])
                sessions[0]["operation"] = operation
                status, payload = self.prepare_raw(
                    {"change_request": self.change_request(sessions=sessions)}
                )
                self.assertEqual(400, status)
                self.assertIn("operation", payload["detail"])

    def test_a_first_plan_cannot_describe_a_plan_it_is_replacing(self):
        for field, value in (
            ("reason_codes", ["athlete_reported_constraint"]),
            ("goal_effect", {"direction": "unchanged", "note": "n"}),
            ("next_review_condition", "after the first week"),
        ):
            with self.subTest(field=field):
                status, payload = self.prepare_raw(
                    {"change_request": self.change_request(**{field: value})}
                )
                self.assertEqual(400, status)
                self.assertIn(field, payload["detail"])

    def test_a_first_plan_cannot_name_a_plan_that_does_not_exist(self):
        for field, value in (("plan_id", "plan-made-up"), ("plan_version", 1)):
            with self.subTest(field=field):
                status, payload = self.prepare_raw(
                    {"change_request": self.change_request(), field: value}
                )
                self.assertEqual(400, status)
                self.assertIn(field, payload["detail"])

    def test_the_preview_ids_echoed_back_are_refused_by_the_apply_half_too(self):
        """The last step of onboarding, and the one shape it kept failing in.

        The preview answers with the `plan_id` and `plan_version` it derived, and every
        other prepare/apply pair in this contract is confirmed by sending the ids the
        preview handed back. A model following that pattern here was read as a change
        against a plan that does not exist and answered by whichever field the change
        path missed first -- a sentence about `context` while the actual fault was a
        `plan_id` the account has no plan to match. Both halves now name the field to
        drop, and neither writes anything.
        """
        _, prepared = self.prepare()
        named = {"plan_id": prepared["plan_id"], "plan_version": prepared["plan_version"]}

        for route, rest in (
            ("prepare", {}),
            ("apply", {"proposal": prepared["proposal"], "confirmed": True}),
        ):
            with self.subTest(route=route):
                status, payload = self.route(
                    f"decision_{route}",
                    body={"change_request": self.change_request(), **named, **rest},
                    token=TOKEN_A,
                )

                self.assertEqual(400, status, payload)
                self.assertEqual("invalid_request", payload["error"])
                self.assertIn("this account has no plan yet", payload["detail"])
                self.assertIn("omit it to author the first plan", payload["detail"])
        self.assertFalse(self.state_dir.exists())

    def test_a_field_this_contract_does_not_have_is_refused_not_dropped(self):
        status, payload = self.prepare_raw(
            {"change_request": self.change_request(decision_event={"id": "x"})}
        )
        self.assertEqual(400, status)
        self.assertIn("decision_event", payload["detail"])
        self.assertFalse((self.state_dir / "store.json").exists())

    def test_a_top_level_field_this_contract_does_not_have_is_refused_too(self):
        """The same rule one level up, where the translation used to drop silently.

        Only four of the body's fields mean anything while authoring a first plan. The
        rest were read past without a word, so a model that believed it had sent
        something got a plan built without it and no way to tell.
        """
        for route, rest in (
            ("prepare", {}),
            ("apply", {"proposal": "x", "confirmed": True}),
        ):
            with self.subTest(route=route):
                status, payload = self.route(
                    f"decision_{route}",
                    body={
                        "change_request": self.change_request(),
                        "decision_event": {"id": "x"},
                        **rest,
                    },
                    token=TOKEN_A,
                )

                self.assertEqual(400, status, payload)
                self.assertEqual("invalid_request", payload["error"])
                self.assertIn("decision_event", payload["detail"])
        self.assertFalse(self.state_dir.exists())

    def test_a_first_plan_cannot_carry_a_context_that_does_not_exist_yet(self):
        """The dropped field that mattered most, and where the athlete's symptom belongs.

        `startCoachSession` returns no context for an empty account, so a context
        arriving here was assembled rather than passed back -- and one carrying
        `constraints.red_flags`, exactly where a real CoachContext carries them, put a
        stated symptom somewhere nothing on this path reads. That request used to
        return 200 with a first week built as though the athlete had said nothing.
        """
        for route, rest in (
            ("prepare", {}),
            ("apply", {"proposal": "x", "confirmed": True}),
        ):
            with self.subTest(route=route):
                status, payload = self.route(
                    f"decision_{route}",
                    body={
                        "change_request": self.change_request(),
                        "context": {"constraints": {"red_flags": {"chest_pain": True}}},
                        **rest,
                    },
                    token=TOKEN_A,
                )

                self.assertEqual(400, status, payload)
                self.assertEqual("invalid_request", payload["error"])
                self.assertIn("red_flags", payload["detail"])
        self.assertFalse(self.state_dir.exists())

    def test_a_first_plan_needs_sessions_it_can_read(self):
        for sessions in ("每週三次", {"monday": "easy"}, None, 3):
            with self.subTest(sessions=sessions):
                status, payload = self.prepare_raw(
                    {"change_request": self.change_request(sessions=sessions)}
                )
                self.assertEqual(400, status, payload)
                self.assertIn("change_request.sessions", payload["detail"])
        self.assertFalse(self.state_dir.exists())

    def test_a_first_plan_session_that_is_not_an_object_is_named_by_position(self):
        sessions = copy.deepcopy(self.change_request()["sessions"])
        sessions.insert(1, "週三休息")

        status, payload = self.prepare_raw(
            {"change_request": self.change_request(sessions=sessions)}
        )

        self.assertEqual(400, status, payload)
        self.assertIn("change_request.sessions[1]", payload["detail"])
        self.assertFalse(self.state_dir.exists())

    def test_a_first_plan_needs_the_week_intent_it_is_built_around(self):
        for week in (None, "一週三次", {}, {"intent": None}, {"intent": "   "}):
            with self.subTest(week=week):
                status, payload = self.prepare_raw(
                    {"change_request": self.change_request(week=week)}
                )
                self.assertEqual(400, status, payload)
                self.assertIn("change_request.week.intent", payload["detail"])
        self.assertFalse(self.state_dir.exists())

    def test_the_first_plan_reaches_the_store_through_the_one_contract(self):
        status, prepared = self.prepare_raw({"change_request": self.change_request()})
        self.assertEqual(200, status, prepared)
        self.assertTrue(prepared["confirmation_required"])

        status, applied = self.route(
            "decision_apply",
            body={
                "change_request": self.change_request(),
                "proposal": prepared["proposal"],
                "confirmed": True,
            },
            token=TOKEN_A,
        )
        self.assertEqual(200, status, applied)
        self.assertEqual(1, applied["plan_version"])
        self.assertEqual(
            1, read_current_plan(self.state_dir)["current_version"]
        )

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

        status, session = self.route("session", body={}, token=TOKEN_A)

        self.assertEqual(200, status)
        self.assertEqual("passed", session["status"])
        self.assertEqual("fixture-plan-001", session["plan_state"]["plan_id"])
        self.assertEqual(plan["goal"], session["plan_state"]["current_plan"]["goal"])

    def test_a_second_athlete_initializes_a_disjoint_store(self):
        _, prepared = self.prepare()
        self.initialize(prepared["proposal"])

        other_owner = self.seed_owner(TOKEN_B, athlete_id="i2")
        other = onboarding(week_intent="這位運動員一週只練兩次")
        status, applied = self.route(
            "decision_apply",
            body={
                "change_request": as_change_request(other),
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


class FirstPlanSymptomBoundaryTests(GatewayTestCase):
    """Issue #19: what the athlete says about today has to reach their first plan too.

    Placed between the two classes it compares, because every test here is one claim
    about both routes at once. Two accounts, one symptom, one day: a settled athlete
    asking for an ordinary weekly change, and a new athlete asking for a first week --
    said in the same words, in the same turn, and until now answered differently, since
    an account with no PlanState has no CoachContext for a red flag to travel in.
    """

    def setUp(self):
        super().setUp()
        # The new athlete owns nothing at all; the settled one owns the fixture plan,
        # whose today (2026-08-13) already holds a quality run.
        self.new_athlete = self.seed_owner(TOKEN_A)
        self.state_dir = self.owner_dir(self.new_athlete)
        self.before = load("plan-state-v1.json")
        self.settled_athlete = self.seed_owner(TOKEN_B, athlete_id="i2", plan=self.before)
        self.context = load("coach-context-day-4.json")
        self.context["constraints"]["red_flags"]["chest_pain"] = True

    def first_plan(
        self, route: str, *, request: dict[str, Any] | None = None, **body: Any
    ) -> tuple[int, Any]:
        payload: dict[str, Any] = {
            "change_request": as_change_request(
                onboarding_starting_today() if request is None else request
            )
        }
        payload.update(body)
        return self.route(f"decision_{route}", body=payload, token=TOKEN_A)

    def weekly_change(self, route: str, **body: Any) -> tuple[int, Any]:
        payload: dict[str, Any] = {
            "plan_id": self.before["plan_id"],
            "plan_version": self.before["version"],
            "context": self.context,
            "change_request": WEEKLY_CHANGE,
        }
        payload.update(body)
        return self.route(f"decision_{route}", body=payload, token=TOKEN_B)

    def symptom_refusal(self, payload: dict[str, Any]) -> str:
        """The single sentence this boundary refuses with, from either route."""
        stated = [
            error
            for error in payload["validation"]["errors"]
            if "explicit red flag" in error
        ]
        self.assertEqual(1, len(stated), payload["validation"]["errors"])
        return stated[0]

    def test_a_first_plan_authored_under_a_symptom_is_refused_the_way_a_change_is(self):
        """The defect: the first plan was the one plan no stated symptom could reach.

        The expected refusal is read out of the change path's own answer rather than
        written down here, so the two cannot quietly become two rules -- and the first
        plan is refused with the same status, the same error code and the same sentence,
        naming its own session on the same day.
        """
        change_status, refused_change = self.weekly_change("prepare")
        self.assertEqual(422, change_status, refused_change)
        limit, _, _ = self.symptom_refusal(refused_change).partition(
            "; this plan still trains today: "
        )

        status, refused_first_plan = self.first_plan(
            "prepare", red_flags={"chest_pain": True}
        )

        self.assertEqual(change_status, status, refused_first_plan)
        self.assertEqual(refused_change["error"], refused_first_plan["error"])
        refusal = self.symptom_refusal(refused_first_plan)
        self.assertTrue(refusal.startswith(limit), (refusal, limit))
        self.assertTrue(refusal.endswith("running-2026-08-13 running"), refusal)
        self.assertFalse(self.state_dir.exists())

    def test_the_same_symptom_leaves_a_first_plan_that_rests_today_open(self):
        """The false-positive control, and the answer the boundary steers toward.

        Identical symptom and identical opening day. This first week rests today and
        trains on Saturday, which is what starting a block while something hurts looks
        like -- and it previews, confirms and commits like any other.
        """
        request = onboarding_starting_today(
            rest_day(), easy_run(scheduled_date="2026-08-15")
        )

        status, prepared = self.first_plan(
            "prepare", request=request, red_flags={"chest_pain": True}
        )
        self.assertEqual(200, status, prepared)

        status, applied = self.first_plan(
            "apply",
            request=request,
            red_flags={"chest_pain": True},
            proposal=prepared["proposal"],
            confirmed=True,
        )

        self.assertEqual(200, status, applied)
        self.assertEqual(1, applied["plan_version"])
        self.assertEqual(1, read_current_plan(self.state_dir)["current_version"])

    def test_an_athlete_who_stated_nothing_authors_the_identical_first_plan(self):
        """Omission is not an all-clear, and it is not a refusal either.

        The same week that trains today, from an athlete who was never asked, was asked
        and did not answer, or answered no. Every one goes through: only a symptom stated
        as present is evidence, and the rest are the same unknown they were before this
        boundary existed.
        """
        for stated in ({}, {"red_flags": {}}, {"red_flags": {"chest_pain": None}},
                       {"red_flags": {"chest_pain": False, "pain": False}}):
            with self.subTest(stated=stated):
                status, prepared = self.first_plan("prepare", **stated)
                self.assertEqual(200, status, prepared)
                self.assertEqual("passed", prepared["status"])

        # And the last of those confirms without resending anything, because what the
        # athlete said is not part of the plan the proposal binds.
        status, applied = self.first_plan(
            "apply", proposal=prepared["proposal"], confirmed=True
        )
        self.assertEqual(200, status, applied)
        self.assertEqual(1, read_current_plan(self.state_dir)["current_version"])

    def test_the_confirmation_is_judged_on_what_the_athlete_has_said_by_then(self):
        """The apply half answers for itself, and the proposal still binds.

        A symptom reported between the preview and the confirmation refuses the write --
        the plan is unchanged and its proposal is perfectly valid, which is exactly why
        the second half has to ask the question again rather than trust that the first
        half already did.
        """
        status, prepared = self.first_plan("prepare")
        self.assertEqual(200, status, prepared)

        status, refused = self.first_plan(
            "apply",
            red_flags={"chest_pain": True},
            proposal=prepared["proposal"],
            confirmed=True,
        )

        self.assertEqual(422, status, refused)
        self.assertEqual("validation_failed", refused["error"])
        self.assertIn("running-2026-08-13 running", self.symptom_refusal(refused))
        self.assertFalse(self.state_dir.exists())

    def test_a_symptom_sent_with_an_ordinary_change_is_refused_rather_than_dropped(self):
        """The field answers for a first plan only, and says so.

        An account with a plan has a context, and the context is where the boundary
        reads the flag from and what the proposal binds. A symptom stated here instead
        would be ignored -- which is the defect, rearranged -- so the route names where
        it belongs and writes nothing.
        """
        settled_dir = self.owner_dir(self.settled_athlete)
        before_files = self.snapshot(settled_dir)

        for route, rest in (("prepare", {}), ("apply", {"proposal": "x", "confirmed": True})):
            with self.subTest(route=route):
                status, payload = self.weekly_change(
                    route, red_flags={"chest_pain": True}, **rest
                )

                self.assertEqual(400, status, payload)
                self.assertEqual("invalid_request", payload["error"])
                self.assertIn("startCoachSession", payload["detail"])
        self.assertEqual(before_files, self.snapshot(settled_dir))


class GatewayDecisionTests(GatewayTestCase):
    """Weekly changes authored the way a model has to author them.

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
        return self.route("decision_prepare", body=body, token=token)

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
        return self.route("decision_apply", body=body, token=token)

    def head_event(self) -> dict[str, Any]:
        commits = sorted(
            path for path in (self.state_dir / "commits").iterdir() if path.is_dir()
        )
        return json.loads((commits[-1] / "event.json").read_text(encoding="utf-8"))

    def test_a_coaching_decision_cannot_reach_what_the_athlete_stated(self):
        """Issue #164: the coach reads the athlete's own aims and habits, never writes them.

        There is no field on a change request that would carry one, and no code path from
        a decision to ``athlete-evidence.json`` -- so a cycle that changes direction
        leaves both exactly as the athlete left them. That is the difference between a
        milestone the coach owns and a target the athlete does.
        """
        athlete_evidence.record_long_term_goal(
            self.state_dir, metric="VO2max", target="50", now=NOW
        )
        athlete_evidence.record_training_preference(
            self.state_dir, topic="重訓頻率", statement="每週想重訓五次", now=NOW
        )
        before = athlete_evidence.load_evidence(self.state_dir)

        status, prepared = self.prepare()
        self.assertEqual(200, status, prepared)
        status, applied = self.apply(prepared["proposal"])
        self.assertEqual(200, status, applied)

        self.assertEqual(before, athlete_evidence.load_evidence(self.state_dir))

    def test_a_change_that_forgot_its_plan_id_is_answered_by_the_plan_that_exists(self):
        """The other half of one routing question, asked of an account that has a plan.

        A body with no `plan_id` is the shape a first plan arrives in, and this account
        cannot author one. The apply half used to translate it anyway and answer "this
        account has no plan yet, so change_request may not carry goal_effect" -- a
        sentence that is simply false here, and that sends the model to edit the field
        it named instead of to the plan it already has. Both halves now answer with that
        plan: its id, and the version to change from.
        """
        before_files = self.snapshot(self.state_dir)
        _, prepared = self.prepare()

        for route, rest in (
            ("prepare", {}),
            ("apply", {"proposal": prepared["proposal"], "confirmed": True}),
        ):
            with self.subTest(route=route):
                status, payload = self.route(
                    f"decision_{route}",
                    body={
                        "context": self.context,
                        "change_request": WEEKLY_CHANGE,
                        **rest,
                    },
                    token=TOKEN_A,
                )

                self.assertEqual(409, status, payload)
                self.assertEqual("plan_state_exists", payload["error"])
                self.assertEqual(self.before["plan_id"], payload["current_plan_id"])
                self.assertEqual(
                    self.before["version"], payload["current_plan_version"]
                )
        self.assertEqual(before_files, self.snapshot(self.state_dir))

    def test_the_plan_that_exists_refusal_names_every_field_the_retry_needs(self):
        """Issue #303: the refusal named the plan and stopped, so the turn stopped too.

        The two ids are what *exists*. They are not what the retry *needs*: the change
        branch also requires a `context`, and only `startCoachSession` returns one. A
        model resending exactly what this refusal named therefore hit `invalid_request`
        on a field the refusal never mentioned, which is the failure the counter caught
        on 2026-08-27 -- one `plan_state_exists`, one `invalid_request`, no delivery.
        """
        status, payload = self.prepare(plan_id=None)

        self.assertEqual(409, status, payload)
        detail = payload["detail"]
        self.assertIn("startCoachSession", detail)
        self.assertIn("prepareCoachDecision", detail)
        for field in ("plan_id", "plan_version", "context"):
            with self.subTest(field=field):
                self.assertIn(field, detail)

    def test_resending_only_what_the_refusal_names_is_still_not_a_retry(self):
        # Why the detail has to name the third field: the two ids alone, which is what
        # `extra` carries and what a model reading only `extra` would resend, are refused
        # by the missing context before any of them is looked at.
        _, refusal = self.prepare(plan_id=None)

        status, payload = self.route(
            "decision_prepare",
            body={
                "plan_id": refusal["current_plan_id"],
                "plan_version": refusal["current_plan_version"],
                "change_request": WEEKLY_CHANGE,
            },
            token=TOKEN_A,
        )

        self.assertEqual(400, status, payload)
        self.assertEqual("invalid_request", payload["error"])
        self.assertIn("context", payload["detail"])

    def test_doing_what_the_refusal_says_prepares_the_change(self):
        """The control: a detail is only worth adding if following it literally works.

        Without this, any sentence containing the three field names would satisfy the
        assertion above. This walks the recovery in the order the detail states it --
        read the plan, then prepare with the plan id, the version and *that* call's
        context -- and requires it to reach a preview.
        """
        status, refusal = self.prepare(plan_id=None)
        self.assertEqual(409, status, refusal)

        status, session = self.route("session", body={}, token=TOKEN_A)
        self.assertEqual(200, status, session)

        status, prepared = self.route(
            "decision_prepare",
            body={
                "plan_id": session["plan_state"]["plan_id"],
                "plan_version": session["plan_state"]["plan_version"],
                "context": session["context"],
                "change_request": WEEKLY_CHANGE,
            },
            token=TOKEN_A,
        )

        self.assertEqual(200, status, prepared)
        self.assertEqual("passed", prepared["status"])
        self.assertEqual(refusal["current_plan_id"], prepared["plan_id"])
        self.assertEqual(refusal["current_plan_version"], prepared["base_version"])

    def test_a_change_stating_no_symptom_at_all_is_read_as_stating_nothing(self):
        """`red_flags: null` is a declared optional property left unused, not a symptom.

        A structured-output client emits every property its schema declares, so it sends
        the key with a null rather than dropping it. Reading the key instead of the value
        refused the athlete's entire week over a field they never filled in -- while
        `_red_flag_overrides` two functions away already reads the same null as the empty
        object it is.
        """
        status, prepared = self.prepare(red_flags=None)
        self.assertEqual(200, status, prepared)

        status, applied = self.apply(prepared["proposal"], red_flags=None)

        self.assertEqual(200, status, applied)
        self.assertEqual(2, applied["plan_version"])

    # -- the two cases the entry has to survive ---------------------------------------

    def test_a_weekly_change_is_authored_from_one_session_response_and_nothing_else(self):
        """The material-change case, start to finish, with no repository fixture in hand.

        Everything the caller sends comes from the previous response or from coaching
        judgment: the plan id and version it was told, the context it was handed back
        verbatim, and one change request naming a session it read.
        """
        status, session = self.route(
            "session",
            body={
                "all_clear": True,
                "recovery_signals": recovery_signals_upload(),
            },
            token=TOKEN_A,
        )
        self.assertEqual(200, status, session)
        self.assertEqual(
            "client-uploaded:personal-os:recovery_daily+daily_metrics",
            session["context"]["recovery_signals"]["source"],
        )
        plan_state = session["plan_state"]
        thursday = next(
            item
            for item in plan_state["current_plan"]["week"]["sessions"]
            if item["scheduled_date"] == "2026-08-13"
        )
        request = copy.deepcopy(WEEKLY_CHANGE)
        request["sessions"][0]["session_id"] = thursday["session_id"]
        request["evidence"].append(
            {
                "field": "recovery_signals.days",
                "observation": "本次上傳的 8/13 recovery signals 可供模型綜合判斷",
            }
        )
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

        status, prepared = self.route(
            "decision_prepare", body=bundle, token=TOKEN_A
        )
        self.assertEqual(200, status, prepared)
        self.assertTrue(prepared["confirmation_required"])
        self.assertEqual(2, prepared["resulting_version"])

        status, applied = self.route(
            "decision_apply",
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

    def restore_a_fork_over_the_store(self, **session_fields: Any) -> dict[str, Any]:
        """Put a *different* plan at this plan's current version, the way an operator can.

        ``restore-store`` opens the store it restores from and refuses on a lock, a
        hand-off, an open delivery reservation and a maintenance fence -- but it never
        compares that store's history against the destination's, so a snapshot taken
        somewhere else restores over this athlete's plan without either of them noticing
        the fork. ``adopt-owner-store --mode copy`` permits the same divergence on
        purpose. This builds one fork through the real path rather than by editing the
        store's files, so the case under test is the operator action and not a corruption.
        """
        forked = copy.deepcopy(self.before)
        for session in forked["week"]["sessions"]:
            if session["session_id"] == "run-quality-01":
                session.update(session_fields)
        fork_dir = Path(tempfile.mkdtemp(dir=self.state_root)) / "somewhere-else"
        init_store(fork_dir, forked)
        restore_snapshot(fork_dir, self.state_dir, confirm=True)
        return forked

    def test_a_plan_replaced_underneath_the_preview_is_refused_at_the_same_version(self):
        """The fork the resent context does not see, and the version cannot.

        ``purpose`` is chosen because this change request overwrites it: the candidate
        plan and event this apply re-derives are byte-identical whether they are projected
        from the plan the athlete was previewed or from the one now standing in its place,
        so ``after_hash`` and ``event_hash`` both still match. The plan id and version
        match too. Of everything the context is compared against -- ``goal_context``,
        ``athlete_baseline``, and a five-field calendar row -- none carries a session's
        purpose. Only a claim over the whole stored plan can refuse this.
        """
        _, prepared = self.prepare()
        forked = self.restore_a_fork_over_the_store(
            purpose="一個運動員從來沒看過的課程目的"
        )
        restored = self.snapshot(self.state_dir)

        status, payload = self.apply(prepared["proposal"])

        self.assertEqual(409, status, payload)
        self.assertEqual("state_conflict", payload["error"])
        self.assertIn("stored plan was replaced", payload["detail"])
        # Nothing was written, and what is there is the fork rather than a merge of the
        # two: a refusal that half-applied would be worse than one that applied.
        self.assertEqual(restored, self.snapshot(self.state_dir))
        current = read_current_plan(self.state_dir)
        self.assertEqual(1, current["current_version"])
        self.assertEqual(forked, current["current_plan"])

    def test_the_same_fork_is_refused_whichever_invisible_field_it_differs_in(self):
        """Not one lucky field: every field of this session this change request overwrites.

        A fork in ``fallback`` moves the candidate plan, because this request does not
        state one and the projection carries the stored session's forward; a fork in
        ``prescription`` moves the event, which quotes what the session used to say. Those
        two are refused today, by ``after_hash`` and ``event_hash``. ``purpose`` and
        ``priority`` are overwritten outright, so nothing downstream of them moves at all
        -- which is why the claim has to be over the stored plan itself.
        """
        for field, value in (
            ("purpose", "一個運動員從來沒看過的課程目的"),
            ("priority", "optional"),
        ):
            with self.subTest(field=field):
                # Prepared against whatever the previous round left standing, so each
                # round is a fresh confirmation refused by its own fork rather than by
                # the one before it.
                _, prepared = self.prepare()
                self.restore_a_fork_over_the_store(**{field: value})

                status, payload = self.apply(prepared["proposal"])

                self.assertEqual(409, status, payload)
                self.assertIn("stored plan was replaced", payload["detail"])
                self.assertEqual(1, read_current_plan(self.state_dir)["current_version"])

    def test_a_proposal_prepared_by_another_build_of_this_gateway_is_refused(self):
        """One signing key outlives a deploy; the validator behind a preview does not.

        The proposal below is genuinely this gateway's -- same key, same athlete, same
        route, same unexpired lifetime -- and the only thing that moved is which build
        answered. Without the release claim it would apply, and what it would apply is a
        preview computed by projection, prose and a validator that are no longer running.
        """
        self.gateway.config = dataclasses.replace(
            self.config, release_identity=release_identity_for("a" * 40)
        )
        _, prepared = self.prepare()
        before_files = self.snapshot(self.state_dir)
        self.gateway.config = dataclasses.replace(
            self.config, release_identity=release_identity_for("b" * 40)
        )

        status, payload = self.apply(prepared["proposal"])

        self.assertEqual(409, status, payload)
        self.assertEqual("proposal_mismatch", payload["error"])
        self.assertIn("different build of this gateway", payload["detail"])
        self.assertEqual(before_files, self.snapshot(self.state_dir))

    def test_a_release_and_an_unidentified_run_refuse_each_others_proposals(self):
        """Both directions, because a deployment can lose its release variables too.

        A gateway started without them says so rather than claiming to be unbound: its
        proposals do not open against a released build, and a released build's do not open
        against it.
        """
        released = dataclasses.replace(
            self.config, release_identity=release_identity_for("c" * 40)
        )
        for prepared_under, applied_under in ((self.config, released), (released, self.config)):
            with self.subTest(applied_under=applied_under.release_identity is not None):
                self.gateway.config = prepared_under
                _, prepared = self.prepare()
                self.gateway.config = applied_under

                status, payload = self.apply(prepared["proposal"])

                self.assertEqual(409, status, payload)
                self.assertEqual("proposal_mismatch", payload["error"])
                self.assertIn("different build of this gateway", payload["detail"])
                self.assertEqual(1, read_current_plan(self.state_dir)["current_version"])

    def test_a_proposal_issued_before_the_build_binding_is_refused_by_name(self):
        """The in-flight proposal a deploy of this change leaves behind.

        For up to one proposal lifetime after the change lands, a client can hand back a
        confirmation this gateway's key really did sign, carrying neither the plan it was
        prepared against nor the build that prepared it. Applying it would mean writing a
        decision bound by neither -- the whole of what this change adds -- so it is
        refused, and it says which of the two happened: a hash mismatch here would send
        the reader hunting for a corrupted proposal that does not exist.
        """
        _, prepared = self.prepare()
        encoded = prepared["proposal"].split(".")[0]
        claims = json.loads(
            base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)).decode("utf-8")
        )
        for stamp in ("issued_at", "expires_at", "release", "before_hash"):
            claims.pop(stamp)
        older = issue_proposal(claims, key=HMAC_KEY, now=self.now)["proposal"]

        status, payload = self.apply(older)

        self.assertEqual(409, status, payload)
        self.assertEqual("proposal_mismatch", payload["error"])
        self.assertIn("before this gateway began binding", payload["detail"])
        self.assertEqual(1, read_current_plan(self.state_dir)["current_version"])

    def test_the_receipt_carries_the_hash_the_proposal_bound_not_a_second_derivation(self):
        """What the replay path compares against, and where it has to come from.

        A retried apply is recognised by matching the proposal's own ``context_hash``
        against the stored receipt. Deriving the receipt's copy from the context object
        instead would leave two derivations of one value with nothing holding them
        together, and the replay would start failing on the day they disagreed.
        """
        _, prepared = self.prepare()
        encoded = prepared["proposal"].split(".")[0]
        claims = json.loads(
            base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)).decode("utf-8")
        )

        status, applied = self.apply(prepared["proposal"])
        self.assertEqual(200, status, applied)

        receipt = read_current_plan(self.state_dir)["receipt"]
        self.assertEqual(claims["context_hash"], receipt["context_hash"])
        self.assertEqual(claims["before_hash"], canonical_hash(self.before))

        status, replayed = self.apply(prepared["proposal"])
        self.assertEqual(200, status, replayed)
        self.assertTrue(replayed["idempotent_replay"])
        self.assertEqual(applied["event_id"], replayed["event_id"])

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
    reaches it, through the real HTTP surface a client uses.
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
        return self.route("decision_prepare", body=body, token=token)

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
        return self.route("decision_apply", body=body, token=token)

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
        self.fake.sport_settings = copy.deepcopy(RUN_SPORT_SETTINGS)

    def prepare_set(self, session_ids: list[str] | None = None) -> dict[str, Any]:
        status, payload = self.route(
            "delivery_prepare",
            body={
                "plan_id": self.plan["plan_id"],
                "plan_version": self.plan["version"],
                "session_ids": session_ids or ["run-quality-01", "run-long-01"],
            },
            token=TOKEN_A,
        )
        self.assertEqual(200, status, payload)
        return payload

    def test_a_delivery_confirmed_after_a_deploy_still_publishes(self):
        """The one confirmed write that is not bound to a build, and why it is not.

        A delivery is confirmed against the ``delivery_set`` itself: the client holds the
        whole thing, hands it all back, and ``approve_delivery_set`` re-derives
        ``proposal_hash`` over exactly what it was given. There is no signed proposal to
        stamp a build into, because there is nothing the server has to remember -- the
        material *is* the binding, and a set edited after the preview stops hashing to
        what was confirmed whichever build re-derives it.

        So a build change between preparing a delivery and confirming it changes nothing
        here, and this says so out loud rather than leaving it to be inferred from a
        refusal that never comes.
        """
        self.gateway.config = dataclasses.replace(
            self.config, release_identity=release_identity_for("a" * 40)
        )
        prepared = self.prepare_set()
        self.gateway.config = dataclasses.replace(
            self.config, release_identity=release_identity_for("b" * 40)
        )

        status, payload = self.route(
            "delivery_apply",
            body={
                "delivery_set": prepared["delivery_set"],
                "proposal_hash": prepared["proposal_hash"],
                "confirmed": True,
            },
            token=TOKEN_A,
        )

        self.assertEqual(200, status, payload)
        self.assertEqual("intervals_accepted", payload["delivery_state"])
        self.assertEqual(
            ["run-quality-01", "run-long-01"],
            [item["session_id"] for item in payload["delivered"]],
        )

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

    def test_a_present_threshold_pace_is_read_but_never_rewritten(self):
        prepared = self.prepare_set()

        self.assertEqual([], prepared["settings_changes"])

        status, payload = self.route(
            "delivery_apply",
            body={
                "delivery_set": prepared["delivery_set"],
                "proposal_hash": prepared["proposal_hash"],
                "confirmed": True,
            },
            token=TOKEN_A,
        )

        self.assertEqual(200, status, payload)
        self.assertEqual(
            3, len([call for call in self.fake.calls if call[1].endswith("/sport-settings")])
        )
        self.assertEqual([], self.fake.settings_updates)
        self.assertEqual(2, len(self.fake.bulk_calls))
        self.assertEqual("intervals_accepted", payload["delivery_state"])
        self.assertEqual("intervals_accepted", payload["max_delivery_state"])

    def test_a_missing_threshold_pace_is_confirmed_written_and_read_back_before_delivery(self):
        self.fake.sport_settings = [{"types": ["Run"], "threshold_pace": None}]
        prepared = self.prepare_set()

        self.assertEqual(1, len(prepared["settings_changes"]))
        self.assertEqual("threshold_pace", prepared["settings_changes"][0]["field"])
        self.assertEqual(370, prepared["settings_changes"][0]["proposed_seconds_per_km"])

        status, payload = self.route(
            "delivery_apply",
            body={
                "delivery_set": prepared["delivery_set"],
                "proposal_hash": prepared["proposal_hash"],
                "confirmed": True,
            },
            token=TOKEN_A,
        )

        self.assertEqual(200, status, payload)
        self.assertEqual([{"threshold_pace": 2.702703}], self.fake.settings_updates)
        self.assertEqual("updated", payload["settings_changes"][0]["operation"])
        self.assertEqual(2, len(self.fake.bulk_calls))

    def test_settings_update_denial_names_the_individually_unticked_permission(self):
        self.fake.sport_settings = [{"types": ["Run"], "threshold_pace": None}]
        prepared = self.prepare_set()
        self.fake.settings_write_status = 403

        status, payload = self.route(
            "delivery_apply",
            body={
                "delivery_set": prepared["delivery_set"],
                "proposal_hash": prepared["proposal_hash"],
                "confirmed": True,
            },
            token=TOKEN_A,
        )

        self.assertEqual(409, status, payload)
        self.assertIn("Settings update", payload["detail"])
        self.assertEqual([], self.fake.bulk_calls)

    def test_a_threshold_pace_added_after_preview_is_not_overwritten(self):
        self.fake.sport_settings = [{"types": ["Run"], "threshold_pace": None}]
        prepared = self.prepare_set()
        self.fake.sport_settings[0]["threshold_pace"] = 3.1

        status, payload = self.route(
            "delivery_apply",
            body={
                "delivery_set": prepared["delivery_set"],
                "proposal_hash": prepared["proposal_hash"],
                "confirmed": True,
            },
            token=TOKEN_A,
        )

        self.assertEqual(409, status, payload)
        self.assertIn("changed after", payload["detail"])
        self.assertEqual([], self.fake.settings_updates)
        self.assertEqual([], self.fake.bulk_calls)

    def test_one_confirmation_publishes_every_selected_workout(self):
        prepared = self.prepare_set()

        status, payload = self.route(
            "delivery_apply",
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

        status, payload = self.route(
            "delivery_apply",
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

        status, payload = self.route(
            "delivery_apply",
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

        status, payload = self.route(
            "delivery_apply",
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
        status, payload = self.route(
            "delivery_prepare",
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


def tuple_of_status(response: tuple[int, dict]) -> tuple[int, str]:
    """`(HTTP status, body status)` -- the pair a readiness assertion actually cares about."""
    status, payload = response
    return status, payload["status"]


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
        commit = "a" * 40
        domain = "https://gateway.example"
        identity = {
            "git_commit": commit,
            "instructions_sha256": instructions,
            # The two digests readiness recomputes rather than takes on trust are the
            # real ones; a literal here would make this test assert "blocked".
            "tool_catalogue_sha256": tool_catalogue_sha256(),
            "skill_sha256": "2" * 64,
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
        # The human-quotable version rides beside the hashes, and matches what MCP
        # initialize tells a client, so "which version is live" has one answer.
        self.assertEqual(PRODUCT_VERSION, payload["product_version"])
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
            "tool_catalogue_sha256": tool_catalogue_sha256(),
            "skill_sha256": "2" * 64,
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

    def test_readiness_refuses_a_tool_catalogue_that_is_not_the_declared_one(self):
        """Issue #117 item 6: the catalogue is recomputed, not taken on trust.

        `tool_catalogue_sha256` is bound into `release_id`, so a wrong value could only
        arrive with a matching `release_id` -- which is exactly the case a deployer who
        rebuilt the bundle from a different tree produces. What makes it visible is that
        readiness rebuilds the catalogue from the running `TOOLS` and compares, the way it
        already does for the package artifact.
        """
        release = {
            "git_commit": "a" * 40,
            "instructions_sha256": "1" * 64,
            "tool_catalogue_sha256": "3" * 64,
            "skill_sha256": "2" * 64,
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
            deployed_git_commit=release["git_commit"],
        )

        self.assertEqual((503, "blocked"), tuple_of_status(self.call("GET", "/readyz")))

        # The same deployment with the catalogue it actually serves goes ready, so the
        # refusal above is the catalogue and nothing else about this configuration.
        agreeing = {**release, "tool_catalogue_sha256": tool_catalogue_sha256()}
        agreeing["release_id"] = make_release_id(
            **{key: value for key, value in agreeing.items() if key != "release_id"}
        )
        self.gateway.config = dataclasses.replace(
            self.gateway.config, release_identity=agreeing
        )
        self.assertEqual((200, "ok"), tuple_of_status(self.call("GET", "/readyz")))

    def test_unknown_path_and_wrong_method_are_refused_without_authentication(self):
        self.assertEqual(
            (404, {"status": "blocked", "error": "not_found"}), self.call("GET", "/nope")
        )
        # `/healthz` is the anonymous half of the same statement: a path that exists,
        # asked for with a method it does not serve, is refused on the method alone --
        # no bearer is read, so no owner and no provider is reached either.
        status, payload = self.call("POST", "/healthz", body={})
        self.assertEqual(405, status)
        self.assertEqual("method_not_allowed", payload["error"])

    def test_oversized_and_wrongly_typed_bodies_are_refused(self):
        self.seed_owner(TOKEN_A, plan=publishable_plan())
        bearer = self.mcp_bearer(TOKEN_A)
        status, payload = self.call(
            "POST", MCP_PATH, raw=b"x" * (1024 * 1024 + 1), token=bearer
        )
        self.assertEqual(413, status)
        self.assertEqual("payload_too_large", payload["error"])

        status, payload = self.call(
            "POST",
            MCP_PATH,
            raw=b"grant_type=x",
            content_type="application/x-www-form-urlencoded",
            token=bearer,
        )
        self.assertEqual(415, status)
        self.assertEqual("unsupported_media_type", payload["error"])
        self.assertEqual([], self.fake.calls)

    def test_logs_and_error_bodies_carry_no_credential_material(self):
        owner_id = self.seed_owner(TOKEN_A, plan=publishable_plan())
        bodies = [
            self.call("GET", "/healthz")[1],
            self.call(
                "POST",
                MCP_PATH,
                body=self.tool_rpc("startCoachSession"),
                token=UNKNOWN_TOKEN,
            )[1],
            self.call(
                "POST",
                MCP_PATH,
                body=self.tool_rpc("prepareCoachDecision"),
                token=self.mcp_bearer(TOKEN_A),
            )[1],
            self.route("decision_prepare", body={}, token=TOKEN_A)[1],
            self.route("delivery_prepare", body={"plan_id": "x"}, token=TOKEN_A)[1],
        ]

        logged = "\n".join(self.log_handler.records)
        self.assertTrue(logged)
        for secret in (TOKEN_A, TOKEN_B, UNKNOWN_TOKEN, CLIENT_SECRET_VALUE):
            self.assertNotIn(secret, logged)
            self.assertNotIn(secret, json.dumps(bodies))
        self.assertNotIn(HMAC_KEY.decode("ascii"), logged)
        # Requests remain traceable without a stable cross-request owner identifier. The
        # two classes are both here because the line is the only place they are told
        # apart, and an unauthenticated request must not be logged as an authenticated
        # one just because it named a tool.
        self.assertIn("POST /mcp -> 401 access=anonymous", logged)
        self.assertIn("POST /mcp -> 200 access=authenticated", logged)
        self.assertNotIn(owner_id, logged)

    def test_the_access_line_survives_a_failure_that_reaches_nobody(self):
        """A request that ends in a `500` is still one line, still classified.

        The line is written after the answer whatever the answer was, so a handler that
        raised something nobody planned for stays as traceable as one that returned --
        with the cause in the exception log beside it and nothing of it in the body.
        """
        self.seed_owner(TOKEN_A, plan=publishable_plan())
        with mock.patch.object(
            self.gateway, "route", side_effect=RuntimeError("provider-secret-in-the-cause")
        ):
            status, payload = self.call(
                "POST",
                MCP_PATH,
                body=self.tool_rpc("getCoachState"),
                token=self.mcp_bearer(TOKEN_A),
            )

        self.assertEqual(500, status)
        self.assertEqual({"status": "blocked", "error": "internal_error"}, payload)
        logged = "\n".join(self.log_handler.records)
        self.assertIn("POST /mcp -> 500 access=authenticated", logged)
        self.assertNotIn("provider-secret-in-the-cause", json.dumps(payload))



class InfrastructureFailureBoundaryTests(GatewayTestCase):
    """What a volume failure is allowed to say to the model that asked (issue #282).

    The two truths this separates are not "safe" and "unsafe" text. They are *who wrote
    the sentence*. This repository's own refusals -- a locked store, a stale plan, a
    weekday nobody has -- name a repair and stay verbatim. The operating system's
    refusals name the absolute path they failed on, which on the hosted deployment is
    the state root plus the opaque owner id that the product otherwise never hands out,
    so they are reduced to their `errno` before anybody outside this process sees them.
    """

    def setUp(self):
        super().setUp()
        self.owner_id = self.seed_owner(TOKEN_A, plan=publishable_plan())
        self.state_dir = self.owner_dir(self.owner_id)

    def assertNothingLeaked(self, answer: Any) -> None:
        rendered = json.dumps(answer, ensure_ascii=False)
        for material in LEAKY_MATERIAL:
            self.assertNotIn(material, rendered)
        self.assertNotIn("Errno", rendered)

    def assertNothingLoggedLeaked(self) -> None:
        """The security log is not where the redacted half goes instead."""
        for event in self.security_events():
            rendered = json.dumps(event, ensure_ascii=False)
            for material in LEAKY_MATERIAL:
                self.assertNotIn(material, rendered)

    # -- reads -------------------------------------------------------------------------

    def test_a_plan_file_the_volume_refuses_says_so_without_saying_where_it_lives(self):
        with unreadable("store.json"):
            status, payload = self.route("state", token=TOKEN_A)

        self.assertEqual(409, status, payload)
        self.assertEqual("state_conflict", payload["error"])
        # Still a usable sentence: which fact could not be read, and why.
        self.assertEqual("cannot read store.json: permission was refused", payload["detail"])
        self.assertNothingLeaked(payload)
        self.assertNothingLoggedLeaked()

    def test_the_same_refusal_over_mcp_carries_no_more_than_the_dispatch_did(self):
        with unreadable("store.json"):
            status, payload = self.call(
                "POST",
                MCP_PATH,
                body=self.tool_rpc("getCoachState"),
                token=self.mcp_bearer(TOKEN_A),
            )

        self.assertEqual(200, status, payload)
        self.assertNothingLeaked(payload)
        self.assertNothingLoggedLeaked()
        self.assertIn("cannot read store.json", json.dumps(payload, ensure_ascii=False))

    def test_an_unreadable_evidence_file_never_names_the_owner_it_belongs_to(self):
        status, _ = self.route(
            "availability_record",
            body={"recurring": {"available_days": ["tue", "thu"]}},
            token=TOKEN_A,
        )
        self.assertEqual(200, status)
        self.assertTrue((self.state_dir / "athlete-evidence.json").is_file())

        with unreadable("athlete-evidence.json"):
            status, payload = self.route("session", body={}, token=TOKEN_A)

        self.assertEqual(409, status, payload)
        self.assertEqual(
            "cannot read athlete-evidence.json: permission was refused", payload["detail"]
        )
        self.assertNothingLeaked(payload)
        self.assertNothingLoggedLeaked()

    def test_a_file_this_product_wrote_badly_still_says_where_in_the_file(self):
        # The control for the read path. A JSON syntax error describes the *contents* of
        # a product-owned file -- a line and a column -- which names a real repair and
        # says nothing about where the deployment keeps it. It stays specific.
        (self.state_dir / "store.json").write_text("{ not json", encoding="utf-8")

        status, payload = self.route("state", token=TOKEN_A)

        self.assertEqual(409, status, payload)
        self.assertIn("cannot read store.json:", payload["detail"])
        self.assertIn("line 1", payload["detail"])

    # -- writes ------------------------------------------------------------------------

    def test_a_write_that_runs_out_of_volume_names_the_volume_not_the_path(self):
        with unwritable("athlete-evidence.json"):
            status, payload = self.route(
                "availability_record",
                body={"recurring": {"available_days": ["tue", "thu"]}},
                token=TOKEN_A,
            )

        self.assertEqual(409, status, payload)
        self.assertEqual(
            "cannot write athlete-evidence.json: the state volume is out of space",
            payload["detail"],
        )
        self.assertNothingLeaked(payload)
        self.assertNothingLoggedLeaked()

    def test_a_store_that_is_already_locked_keeps_the_sentence_that_names_the_fix(self):
        # The control for the write path: this repository's own refusal, unchanged.
        (self.state_dir / ".lock").write_text("pid=1\n", encoding="utf-8")

        status, payload = self.route(
            "availability_record",
            body={"recurring": {"available_days": ["tue"]}},
            token=TOKEN_A,
        )

        self.assertEqual(409, status, payload)
        self.assertEqual("state store is locked by another operation", payload["detail"])

    def test_a_weekday_nobody_has_is_still_reported_as_the_weekday_it_was(self):
        # The other control: athlete input the coach can fix by asking again.
        status, payload = self.route(
            "availability_record",
            body={"recurring": {"available_days": ["someday"]}},
            token=TOKEN_A,
        )

        self.assertEqual(400, status, payload)
        self.assertIn("someday", payload["detail"])

    # -- the one store file named after its owner ---------------------------------------

    def write_fence(self, record: dict[str, Any]) -> Path:
        """One maintenance fence on disk, without holding the store still to get it there.

        ``owner_maintenance_fence`` would refuse the write under test before the read
        under test could fail, and what these two assert is the *reading* of the file.
        """
        path = maintenance_fence_path(self.state_dir)
        path.write_text(json.dumps(record), encoding="utf-8")
        return path

    def test_an_unreadable_maintenance_fence_never_names_the_owner_it_was_holding(self):
        """The fence is the one store file whose filename is not product-owned.

        It lives beside the owner directory so that it can outlive one, so it is named
        after that directory -- the opaque owner id on the hosted deployment. Every write
        reads it, and a read that fails on the volume turns into a `state_conflict` the
        client can see, so naming the file here would hand out the id (issue #282).
        """
        fence = self.write_fence(
            {
                "schema_version": MAINTENANCE_FENCE_SCHEMA_VERSION,
                "fence_id": "owner-maintenance-0123456789abcdef01234567",
                "operation": "archive-store",
                "acquired_at": "2026-08-27T00:00:00Z",
            }
        )
        self.assertIn(self.owner_id, fence.name)

        with unreadable(fence.name):
            status, payload = self.route(
                "availability_record",
                body={"recurring": {"available_days": ["tue", "thu"]}},
                token=TOKEN_A,
            )

        self.assertEqual(409, status, payload)
        self.assertEqual("state_conflict", payload["error"])
        # Still the usable half: which file could not be read, and why. Named by what it
        # is, because which owner it was holding was never the part a caller could act on.
        self.assertEqual(
            "cannot read the maintenance fence: permission was refused", payload["detail"]
        )
        self.assertNotIn(self.owner_id, json.dumps(payload, ensure_ascii=False))
        self.assertNothingLeaked(payload)
        self.assertNothingLoggedLeaked()

    def test_a_malformed_maintenance_fence_is_refused_without_naming_it_either(self):
        """The same filename reaches the client with no volume failure at all.

        A fence this code cannot read raises rather than reading as absent -- "absent" is
        the one answer that would let a write through the gate that exists to stop it --
        so a fence written by a newer checkout refuses every write and says so. That
        sentence is client-visible on an ordinary disk, which is what makes it the cheaper
        half of the same leak.
        """
        fence = self.write_fence({"schema_version": "9.9"})

        status, payload = self.route(
            "availability_record",
            body={"recurring": {"available_days": ["tue"]}},
            token=TOKEN_A,
        )

        self.assertEqual(409, status, payload)
        self.assertEqual(
            f"the maintenance fence schema_version must be "
            f"{MAINTENANCE_FENCE_SCHEMA_VERSION}",
            payload["detail"],
        )
        self.assertNotIn(self.owner_id, json.dumps(payload, ensure_ascii=False))
        # The store is untouched: the fence refused the write, it did not consume it.
        self.assertTrue(fence.is_file())

    def test_a_fence_that_is_holding_the_store_still_says_so_in_full(self):
        """The control. Only the *filename* is withheld; the holder message is unchanged.

        This is the sentence an operator acts on, and it is this repository's own
        wording rather than the operating system's, so it stays verbatim.
        """
        self.write_fence(
            {
                "schema_version": MAINTENANCE_FENCE_SCHEMA_VERSION,
                "fence_id": "owner-maintenance-0123456789abcdef01234567",
                "operation": "archive-store",
                "acquired_at": "2026-08-27T00:00:00Z",
                "held_by_pid": 4321,
            }
        )

        status, payload = self.route(
            "availability_record",
            body={"recurring": {"available_days": ["tue"]}},
            token=TOKEN_A,
        )

        self.assertEqual(409, status, payload)
        self.assertIn("a maintenance operation is in progress", payload["detail"])
        self.assertIn("archive-store", payload["detail"])
        self.assertNotIn(self.owner_id, json.dumps(payload, ensure_ascii=False))


# The twenty-two paths the coaching REST entry served until issue #288 item 1. Written
# out rather than derived, deliberately: a set derived from `ROUTES` would be empty now
# and the test would pass by asserting nothing. This list is the only remaining record
# that these spellings were once reachable from the internet.
RETIRED_COACH_PATHS = (
    "/v1/coach/session",
    "/v1/coach/state",
    "/v1/coach/permissions",
    "/v1/coach/profile",
    "/v1/coach/availability",
    "/v1/coach/long-term-goal",
    "/v1/coach/training-preference",
    "/v1/coach/strength-report",
    "/v1/coach/strength-prescribed",
    "/v1/coach/body-measurement",
    "/v1/coach/activity-summary",
    "/v1/coach/subjective-state",
    "/v1/coach/record/retract",
    "/v1/coach/history/import",
    "/v1/coach/decision/prepare",
    "/v1/coach/decision/apply",
    "/v1/coach/delivery/prepare",
    "/v1/coach/delivery/apply",
    "/v1/coach/delivery/attempt/clear",
    "/v1/coach/data/export",
    "/v1/coach/data/deletion/prepare",
    "/v1/coach/data/deletion/apply",
)


class RetiredRestSurfaceTests(GatewayTestCase):
    """The coaching REST entry is gone, and gone the way an unknown path is gone.

    It accepted the athlete's raw Intervals credential as its bearer -- the one place
    an upstream credential was an identity here -- so what is asserted is not that it
    refuses but that it does not exist: a `404` before any bearer is read, whatever the
    method and whatever is presented. A `401` would be worse than the routes staying,
    because it would say there is something here to authenticate for.
    """

    METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD")

    def test_every_retired_path_is_gone_for_every_method_and_every_bearer(self):
        # A real owner, so that "the raw provider token" below is a credential that
        # would have resolved rather than one that was never going to.
        self.seed_owner(TOKEN_A, plan=publishable_plan())
        before = self.snapshot(self.state_root)
        bearers = {
            "none": None,
            # The credential the retired routes accepted, and the reason they went.
            "raw provider token": TOKEN_A,
            # The credential the one remaining entry accepts, so that "gone" is not
            # quietly "gone unless you hold a real token".
            "this gateway's own access token": self.mcp_bearer(TOKEN_A),
            "unknown": UNKNOWN_TOKEN,
        }
        for path in RETIRED_COACH_PATHS:
            for method in self.METHODS:
                for label, bearer in bearers.items():
                    with self.subTest(path=path, method=method, bearer=label):
                        status, payload = self.call(method, path, token=bearer)
                        self.assertEqual(404, status)
                        # HEAD carries no body by definition; every other method states
                        # the same refusal the code has always used for a missing path.
                        if method != "HEAD":
                            self.assertEqual(
                                {"status": "blocked", "error": "not_found"}, payload
                            )

        # Nothing behind the boundary was touched by any of them: no provider call, not
        # one byte of any owner's state -- the usage counter included, which is the
        # write every authenticated request makes -- and no request logged as
        # authenticated.
        self.assertEqual([], self.fake.calls)
        self.assertEqual(before, self.snapshot(self.state_root))
        logged = "\n".join(self.log_handler.records)
        self.assertNotIn("access=authenticated", logged)

    def test_no_retired_path_carries_a_challenge_back(self):
        """A retired path must not advertise where to authenticate for it."""
        for path in RETIRED_COACH_PATHS:
            with self.subTest(path=path):
                request = urllib.request.Request(
                    self.base_url + path, data=b"{}", method="POST"
                )
                request.add_header("Content-Type", "application/json")
                request.add_header("Authorization", "Bearer " + TOKEN_A)
                with self.assertRaises(urllib.error.HTTPError) as caught:
                    urllib.request.urlopen(request, timeout=10)
                with caught.exception as exc:
                    self.assertEqual(404, exc.code)
                    self.assertNotIn("WWW-Authenticate", dict(exc.headers))

    def test_no_route_table_entry_hands_a_provider_credential_to_a_coaching_act(self):
        """The property the deletion exists for, stated against the table itself.

        `resolve_owner` fingerprints whatever bearer it is given against fingerprints of
        Intervals access tokens, so any route dispatching through it accepts the
        athlete's provider credential as an identity -- bypassing the audience binding
        and the revocation epoch that `resolve_mcp_owner` enforces. No route may reach a
        coaching act any more except through `/mcp`.
        """
        self.assertEqual(
            set(), {kind for _, kind in ROUTES.values()} & CoachGateway.route_kinds()
        )
        self.assertEqual(
            set(),
            {path for path in ROUTES if path.startswith("/v1/")},
        )


class DomainVerificationChallengeTests(GatewayTestCase):
    """Proving control of this domain to a plugin directory, and nothing more.

    The directory fetches the path itself and compares the body byte for byte, so the
    two things worth holding are that the body is the token alone -- no JSON wrapper, no
    second token, no trailing decoration -- and that a deployment with no verification in
    flight looks exactly like one that never had the path.
    """

    def fetch(self) -> tuple[int, str, str]:
        request = urllib.request.Request(
            self.base_url + OPENAI_APPS_CHALLENGE_PATH, method="GET"
        )
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return (
                    response.status,
                    response.headers.get("Content-Type", ""),
                    response.read().decode("utf-8"),
                )
        except urllib.error.HTTPError as exc:
            with exc:
                return exc.code, exc.headers.get("Content-Type", ""), exc.read().decode("utf-8")

    def test_an_unconfigured_deployment_answers_like_any_unknown_path(self):
        status, _, body = self.fetch()

        self.assertEqual(404, status)
        self.assertEqual({"status": "blocked", "error": "not_found"}, json.loads(body))

    def test_a_configured_deployment_returns_that_token_and_nothing_else(self):
        token = "openai-apps-verification-9f2c41d7"
        self.gateway.config = GatewayConfig(
            state_root=self.state_root,
            token_hmac_key=HMAC_KEY,
            intervals_client_id=CLIENT_ID_VALUE,
            intervals_client_secret=CLIENT_SECRET_VALUE,
            openai_apps_challenge=token,
        )

        status, content_type, body = self.fetch()

        self.assertEqual(200, status)
        self.assertEqual("text/plain; charset=utf-8", content_type)
        self.assertEqual(token, body)
        self.assertEqual([], self.fake.calls)
        self.assertFalse((self.state_root / "owners").exists())


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
            "tool_catalogue_sha256": tool_catalogue_sha256(),
            "skill_sha256": "2" * 64,
            "gateway_artifact_sha256": gateway_artifact_sha256(),
            "gateway_domain": "https://gateway.example",
        }
        return {
            RELEASE_ID_ENV_VAR: make_release_id(**release),
            RELEASE_COMMIT_ENV_VAR: release["git_commit"],
            RELEASE_INSTRUCTIONS_SHA_ENV_VAR: release["instructions_sha256"],
            RELEASE_TOOL_CATALOGUE_SHA_ENV_VAR: release["tool_catalogue_sha256"],
            RELEASE_SKILL_SHA_ENV_VAR: release["skill_sha256"],
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
        self.assertIsNone(config.openai_apps_challenge)
        self.assertEqual(0.0, config.startup_drain_seconds)

    def test_the_domain_verification_token_is_optional_and_read_verbatim(self):
        token = "openai-apps-verification-9f2c41d7"

        self.assertIsNone(load_config(self.env).openai_apps_challenge)
        self.assertEqual(
            token,
            load_config(
                {**self.env, OPENAI_APPS_CHALLENGE_ENV_VAR: f"  {token}\n"}
            ).openai_apps_challenge,
        )
        self.assertIsNone(
            load_config({**self.env, OPENAI_APPS_CHALLENGE_ENV_VAR: "   "}).openai_apps_challenge
        )

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

    def test_release_variables_from_before_the_identity_change_say_so(self):
        """Issue #117 item 6: the deploy-ordering failure, from the deployment's side.

        Staging the previous release's variables against this code satisfies every name
        but the two that replaced `openapi_sha256`, and the container refuses to start.
        On the hosted deployment that refusal is an outage rather than a rejection (see
        docs/ops/verify-production-status.md), so it has to name the actual mistake
        instead of reporting a field count.
        """
        staged = {
            key: value
            for key, value in self.release_env().items()
            if key not in (RELEASE_TOOL_CATALOGUE_SHA_ENV_VAR, RELEASE_SKILL_SHA_ENV_VAR)
        }
        staged[LEGACY_RELEASE_OPENAPI_SHA_ENV_VAR] = "2" * 64

        with self.assertRaisesRegex(
            GatewayConfigError, "predate the release-identity change"
        ):
            load_config({**self.env, **staged, **self.deployment_env()})

        # Without the retired variable there is nothing to recognise, so the older, less
        # specific answer is still the right one.
        del staged[LEGACY_RELEASE_OPENAPI_SHA_ENV_VAR]
        with self.assertRaisesRegex(GatewayConfigError, "identity is incomplete"):
            load_config({**self.env, **staged, **self.deployment_env()})

        # And a deployment that carries the retired variable alongside a complete new set
        # is not old, just untidy: the unread name changes nothing.
        config = load_config(
            {
                **self.env,
                **self.release_env(),
                LEGACY_RELEASE_OPENAPI_SHA_ENV_VAR: "2" * 64,
                **self.deployment_env(),
            }
        )
        self.assertIsNotNone(config.release_identity)

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

        base_url = f"http://127.0.0.1:{self.port}"
        bearer = token_envelope.seal(
            {
                "intervals_token": TOKEN_A,
                "aud": base_url + MCP_PATH,
                "scope": ",".join(INTERVALS_OAUTH_SCOPES),
                "iat": int(NOW.timestamp()),
            },
            kind=token_envelope.ACCESS_TOKEN,
            key=HMAC_KEY,
        )

        def do_request() -> None:
            # A tool call that reaches the provider, which is what makes the request
            # slow enough to still be in flight when the signal arrives. This class
            # builds its own server rather than extending `GatewayTestCase`, so the
            # envelope is sealed here rather than through `mcp_bearer`.
            request = urllib.request.Request(
                base_url + MCP_PATH,
                data=json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "tools/call",
                        "params": {"name": "inspectIntervalsPermissions", "arguments": {}},
                    }
                ).encode("utf-8"),
                method="POST",
            )
            request.add_header("Content-Type", "application/json")
            request.add_header("Authorization", "Bearer " + bearer)
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


class CalendarDisagreementTests(unittest.TestCase):
    """Which days are worth saying the watch disagrees about, and which are not.

    Only a day that can still mislead the athlete. A day already trained has its answer,
    and a day already past is a record rather than an instruction -- the product never
    removes it. Today counts as ahead until something has been trained against it.
    """

    def plan(self, **session: Any) -> dict[str, Any]:
        row = {
            "session_id": "run-quality-01",
            "scheduled_date": "2026-08-14",
            "match_status": "planned",
            "execution": {
                "delivery_state": "not_published",
                "publish_supported": True,
                "superseded_external_id": "9001",
            },
        }
        execution = {**row["execution"], **session.pop("execution", {})}
        return {"week": {"sessions": [{**row, **session, "execution": execution}]}}

    def test_a_day_still_ahead_is_named_with_the_way_it_comes_back_in_line(self):
        self.assertEqual(
            [{
                "session_id": "run-quality-01",
                "scheduled_date": "2026-08-14",
                "resolution": "deliver_replacement",
            }],
            _calendar_disagreements(self.plan(), "2026-08-13"),
        )

    def test_a_session_with_nothing_left_to_publish_is_withdrawn_instead(self):
        rows = _calendar_disagreements(
            self.plan(execution={"publish_supported": False}), "2026-08-13"
        )
        self.assertEqual("withdraw", rows[0]["resolution"])

    def test_today_still_counts_as_ahead(self):
        self.assertEqual(1, len(_calendar_disagreements(self.plan(), "2026-08-14")))

    def test_a_day_already_past_is_a_record_and_is_not_named(self):
        self.assertEqual([], _calendar_disagreements(self.plan(), "2026-08-15"))

    def test_a_session_already_trained_has_its_answer(self):
        self.assertEqual(
            [], _calendar_disagreements(self.plan(match_status="completed"), "2026-08-13")
        )

    def test_a_session_the_calendar_never_held_is_not_a_disagreement(self):
        """The control that keeps this from becoming 'you have not pushed this week yet'.

        A day with no entry has no second answer to be confused by, and saying so every
        turn would bury the days that do.
        """
        plan = self.plan(execution={"superseded_external_id": None})
        self.assertEqual([], _calendar_disagreements(plan, "2026-08-13"))


class GatewayWithdrawalTests(GatewayDeliveryTests):
    """Issue #113: the hosted athlete can also remove a workout their change superseded."""

    def _publish_one(self) -> str:
        prepared = self.prepare_set(["run-quality-01"])
        status, payload = self.route(
            "delivery_apply",
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

        status, prepared = self.route(
            "delivery_prepare",
            body={
                "plan_id": current["plan_id"],
                "plan_version": current["current_version"],
                "session_ids": ["run-quality-01"],
                "withdraw": True,
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

        status, payload = self.route(
            "delivery_apply",
            body={
                "delivery_set": prepared["delivery_set"],
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

    def test_a_change_that_leaves_the_watch_showing_something_else_is_said_out_loud(self):
        """The turn that creates the disagreement is the turn that has to name it.

        The facts were already there -- `delivery_state` back to `not_published`,
        `superseded_external_id` keeping the event -- as two fields on one session among
        seven. Read that way they are bookkeeping, and the next time the athlete meets
        them is on the day, on their watch, at the start line.
        """
        self._publish_one()
        _, before = self.route("session", body={}, token=TOKEN_A)
        self.assertNotIn("calendar_disagreements", before["delivery"])

        self._supersede()
        _, after = self.route("session", body={}, token=TOKEN_A)

        self.assertEqual(
            [{"session_id": "run-quality-01", "scheduled_date": "2026-08-13",
              "resolution": "withdraw"}],
            after["delivery"]["calendar_disagreements"],
        )

    def test_the_hosted_preview_describes_the_calendar_entry_being_removed(self):
        """The athlete confirms a deletion by reading what disappears.

        The preview carried a session id, the session's date and an opaque provider id
        -- nothing an athlete could match against their own calendar, and after a move
        the date belonged to the session rather than to the entry being deleted.
        """
        delivered_id = self._publish_one()
        self._supersede()
        current = read_current_plan(self.state_dir)

        status, prepared = self.route(
            "delivery_prepare",
            body={
                "plan_id": current["plan_id"],
                "plan_version": current["current_version"],
                "session_ids": ["run-quality-01"],
                "withdraw": True,
            },
            token=TOKEN_A,
        )

        self.assertEqual(200, status, prepared)
        row = prepared["preview"][0]
        event = next(item for item in self.fake.events if str(item["id"]) == delivered_id)
        self.assertTrue(row["event_present"])
        self.assertEqual(str(event["start_date_local"])[:10], row["event_date"])
        self.assertEqual(event["name"], row["event_name"])
        self.assertEqual([], self.fake.deleted)

    def test_an_entry_edited_after_the_hosted_preview_is_refused_not_deleted(self):
        """The confirmation binds what was shown, the same way a delivery does."""
        delivered_id = self._publish_one()
        self._supersede()
        current = read_current_plan(self.state_dir)

        status, prepared = self.route(
            "delivery_prepare",
            body={
                "plan_id": current["plan_id"],
                "plan_version": current["current_version"],
                "session_ids": ["run-quality-01"],
                "withdraw": True,
            },
            token=TOKEN_A,
        )
        self.assertEqual(200, status, prepared)
        for event in self.fake.events:
            if str(event["id"]) == delivered_id:
                event["name"] = "在確認之間被改掉的名字"

        status, refused = self.route(
            "delivery_apply",
            body={
                "delivery_set": prepared["delivery_set"],
                "proposal_hash": prepared["proposal_hash"],
                "confirmed": True,
            },
            token=TOKEN_A,
        )

        self.assertEqual(409, status, refused)
        self.assertIn("has changed since", refused["detail"])
        self.assertEqual([], self.fake.deleted)

    def test_the_athlete_s_own_timezone_decides_which_days_are_already_past(self):
        # Same defect #112 fixed for status and startCoachSession: the day that decides
        # what may be removed has to be the athlete's, not the server's default.
        self._publish_one()
        self._supersede()
        current = read_current_plan(self.state_dir)
        _, prepared = self.route(
            "delivery_prepare",
            body={
                "plan_id": current["plan_id"],
                "plan_version": current["current_version"],
                "session_ids": ["run-quality-01"],
                "withdraw": True,
            },
            token=TOKEN_A,
        )
        body = {
            "delivery_set": prepared["delivery_set"],
            "proposal_hash": prepared["proposal_hash"],
            "confirmed": True,
        }

        status, payload = self.route(
            "delivery_apply",
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
        _, prepared = self.route(
            "delivery_prepare",
            body={
                "plan_id": current["plan_id"],
                "plan_version": current["current_version"],
                "session_ids": ["run-quality-01"],
                "withdraw": True,
            },
            token=TOKEN_A,
        )
        body = {
            "delivery_set": prepared["delivery_set"],
            "proposal_hash": prepared["proposal_hash"],
            "confirmed": True,
        }

        status, payload = self.route(
            "delivery_apply", body=body, token=TOKEN_A
        )
        self.assertEqual(409, status, payload)
        self.assertEqual([], self.fake.deleted)

        athlete_evidence.record_profile(self.state_dir, timezone="UTC", now=self.now)
        status, payload = self.route(
            "delivery_apply", body=body, token=TOKEN_A
        )
        self.assertEqual(200, status, payload)
        self.assertEqual(["9001"], self.fake.deleted)

    def test_an_unresolvable_timezone_on_a_withdrawal_is_one_actionable_error(self):
        self._publish_one()
        self._supersede()
        current = read_current_plan(self.state_dir)
        _, prepared = self.route(
            "delivery_prepare",
            body={
                "plan_id": current["plan_id"],
                "plan_version": current["current_version"],
                "session_ids": ["run-quality-01"],
                "withdraw": True,
            },
            token=TOKEN_A,
        )

        status, payload = self.route(
            "delivery_apply",
            body={
                "delivery_set": prepared["delivery_set"],
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
        _, prepared = self.route(
            "delivery_prepare",
            body={
                "plan_id": current["plan_id"],
                "plan_version": current["current_version"],
                "session_ids": ["run-quality-01"],
                "withdraw": True,
            },
            token=TOKEN_A,
        )

        status, payload = self.route(
            "delivery_apply",
            body={
                "delivery_set": prepared["delivery_set"],
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
        status, payload = self.route("session", body={}, token=TOKEN_A)
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
        return self.route("profile_record", body=body, token=token)

    def session(self, body: dict[str, Any] | None = None):
        return self.route("session", body=body or {}, token=TOKEN_A)

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

        _, other = self.route("session", body={}, token=TOKEN_B)

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
        status, prepared = self.route(
            "decision_prepare",
            body={"change_request": as_change_request(ONBOARDING)},
            token=TOKEN_A,
        )
        self.assertEqual(200, status, prepared)
        status, applied = self.route(
            "decision_apply",
            body={
                "change_request": as_change_request(ONBOARDING),
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
        self.route("profile_record", body={"language": "en"}, token=TOKEN_A)

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
        self.route("profile_record", body={"language": "en"}, token=TOKEN_A)
        self._initialize()
        current = read_current_plan(self.state_dir)
        strength_id = next(
            session_id
            for session_id, session in self._sessions().items()
            if session["sport"] == "strength"
        )

        status, prepared = self.route(
            "delivery_prepare",
            body={
                "plan_id": current["plan_id"],
                "plan_version": current["current_version"],
                "session_ids": [strength_id],
            },
            token=TOKEN_A,
        )
        self.assertEqual(200, status, prepared)
        status, published = self.route(
            "delivery_apply",
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
        return self.route("availability_record", body=body, token=token)

    def strength(self, body: dict[str, Any], *, token: str | None = TOKEN_A):
        return self.route("strength_report", body=body, token=token)

    def measurement(self, body: dict[str, Any], *, token: str | None = TOKEN_A):
        return self.route("body_measurement_record", body=body, token=token)

    def reported_activity(self, body: dict[str, Any], *, token: str | None = TOKEN_A):
        return self.route("activity_summary_record", body=body, token=token)

    def retract(self, body: dict[str, Any], *, token: str | None = TOKEN_A):
        return self.route("athlete_record_retract", body=body, token=token)

    def import_history(self, body: dict[str, Any], *, token: str | None = TOKEN_A):
        return self.route("history_import", body=body, token=token)

    def long_term_goal(self, body: dict[str, Any], *, token: str | None = TOKEN_A):
        return self.route("long_term_goal_record", body=body, token=token)

    def training_preference(self, body: dict[str, Any], *, token: str | None = TOKEN_A):
        return self.route("training_preference_record", body=body, token=token)

    def subjective_state(self, body: dict[str, Any], *, token: str | None = TOKEN_A):
        return self.route("subjective_state_record", body=body, token=token)

    def session(self, *, token: str | None = TOKEN_A, body: dict[str, Any] | None = None):
        return self.route("session", body=body or {}, token=token)

    # -- the two routes ----------------------------------------------------------------

    def test_an_uploaded_export_reaches_the_next_conversations_context_as_the_athletes_word(self):
        """The whole feature, end to end: a file goes in, a later conversation reads it.

        This is the continuity the two conversational routes are tested for, asked of the
        third way evidence arrives. What comes back has to be labelled as the athlete's
        own -- an upload is their record of their training, not something this product
        observed -- and has to sit outside `recent_actuals` where nothing can attach it
        to a planned session.
        """
        status, payload = self.import_history(
            {
                "format": "csv",
                "source_name": "Strava 匯出",
                "content": (
                    "Activity ID,Activity Date,Activity Name,Activity Type,Elapsed Time,Distance\n"
                    "9001,2026-08-11 06:12:00,晨跑,Run,2700,8.1\n"
                    "9002,2026-08-12 07:00:00,Pool,Swim,2400,1.2\n"
                ),
            }
        )

        self.assertEqual(200, status, payload)
        self.assertEqual("strava", payload["recognised_as"])
        self.assertEqual(2, payload["counts"]["added"])

        _, session = self.session()
        reported = session["context"]["reported_activities"]["activities"]
        self.assertEqual(
            {("2026-08-11", "running"), ("2026-08-12", "swimming")},
            {(row["date"], row["sport"]) for row in reported},
        )
        for row in reported:
            self.assertEqual("athlete_imported", row["source"])
            self.assertEqual("Strava 匯出", row["imported_from"])
        # Never a provider actual, however it arrived.
        self.assertEqual(
            [], [row for row in session["context"]["recent_actuals"] if row["date"] == "2026-08-11"]
        )

    def test_the_same_upload_twice_is_recognised_rather_than_stored_twice(self):
        payload = {
            "format": "csv",
            "content": (
                "Activity ID,Activity Date,Activity Name,Activity Type,Elapsed Time,Distance\n"
                "9001,2026-08-11 06:12:00,晨跑,Run,2700,8.1\n"
            ),
        }

        self.import_history(payload)
        status, second = self.import_history(payload)

        self.assertEqual(200, status, second)
        self.assertTrue(second["already_imported"])
        _, session = self.session()
        self.assertEqual(1, len(session["context"]["reported_activities"]["activities"]))

    def test_a_header_this_does_not_know_asks_for_a_mapping_instead_of_guessing(self):
        status, payload = self.import_history(
            {"format": "csv", "content": "When,What,HowLong\n2026-08-11,Run,45\n"}
        )

        self.assertEqual(400, status)
        self.assertIn("column_mapping", payload["detail"])

    def test_an_upload_is_bound_to_the_credential_and_never_to_another_athlete(self):
        self.seed_owner(TOKEN_B, athlete_id="i2", plan=publishable_plan())
        self.import_history(
            {
                "format": "records",
                "records": [{"date": "2026-08-11", "sport": "running", "duration_minutes": 45}],
            }
        )

        _, theirs = self.session(token=TOKEN_B)

        self.assertIsNone(theirs["context"]["reported_activities"])

    def test_an_upload_is_in_the_export_and_goes_with_a_deletion(self):
        self.import_history(
            {
                "format": "records",
                "source_name": "手動整理",
                "records": [{"date": "2026-08-11", "sport": "running", "duration_minutes": 45}],
            }
        )

        _, export = self.route("data_export", token=TOKEN_A)
        evidence = export["athlete_evidence"]
        self.assertEqual(1, len(evidence["reported_activities"]))
        # The upload itself is in the archive too, as a ledger entry holding no file
        # content -- what was handed over is part of "what do you hold about me".
        self.assertEqual(1, len(evidence["imports"]))
        self.assertEqual("手動整理", evidence["imports"][0]["source_name"])

        _, preview = self.route("deletion_prepare", token=TOKEN_A)
        self.assertEqual(1, preview["removes"]["reported_activities"])
        self.assertEqual(1, preview["removes"]["imported_uploads"])

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
        return self.route("strength_prescribed_confirm", body=body, token=token)

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
            ("availability_record", {}),
            ("availability_record", {"recurring": {"available_days": ["someday"]}}),
            ("availability_record", {"recurring": {"available_days": []}}),
            # A week that has already ended is not a week anyone can plan.
            (
                "availability_record",
                {"week": {"week_start": "2026-08-03", "available_days": ["tue"]}},
            ),
            # The two forms answer different questions; together they have no meaning.
            ("availability_record", {"week": {"only_days": ["tue"], "unavailable_days": ["wed"]}}),
            ("availability_record", {"recurring": {"available_days": ["mon"]}, "recuring": {}}),
            # A movement with no sets reports nothing that was not already known.
            ("strength_report", {"date": "2026-08-12", "exercise": "bench press"}),
            # A day the athlete has not reached yet.
            ("strength_report", {
                "date": "2026-08-20", "exercise": "bench press", "sets": [{"set": 1}],
            }),
        )
        for kind, body in cases:
            with self.subTest(kind=kind, body=body):
                status, payload = self.route(kind, body=body, token=TOKEN_A)
                self.assertEqual(400, status)
                self.assertEqual("invalid_request", payload["error"])
        self.assertFalse((self.state_dir / "athlete-evidence.json").exists())

    def test_neither_route_answers_without_a_token(self):
        for kind in ("availability_record", "strength_report"):
            with self.subTest(kind=kind):
                status, payload = self.route(kind, body={}, token=None)
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
        self.measurement({"weight_kg": 72.5})
        self.reported_activity({"sport": "running", "duration_minutes": 40})

        _, other_session = self.session(token=TOKEN_B)

        constraints = other_session["context"]["constraints"]
        self.assertEqual([], constraints["available_days"])
        self.assertIsNone(constraints["availability_source"])
        self.assertIsNone(other_session["context"]["strength_execution"])
        self.assertIsNone(other_session["context"]["body_measurements"])
        self.assertIsNone(other_session["context"]["reported_activities"])
        self.assertFalse((self.owner_dir(other_owner) / "athlete-evidence.json").exists())

    # -- what the athlete measured, and what no device recorded -------------------------

    def test_a_measurement_is_stored_and_echoed_back_exactly_as_stored(self):
        status, payload = self.measurement({"weight_kg": 72.5})

        self.assertEqual(200, status, payload)
        self.assertEqual("passed", payload["status"])
        self.assertFalse(payload["idempotent_replay"])
        self.assertIsNone(payload["replaced"])
        # The echo is the correction opportunity, so it is the stored record itself --
        # not a restatement of the request, which would agree with a request that was
        # stored wrong.
        stored = athlete_evidence.load_evidence(self.state_dir)["body_measurements"][0]
        self.assertEqual(stored, payload["measurement"])
        self.assertEqual("2026-08-13", payload["measurement"]["date"])
        self.assertEqual("athlete_reported", payload["measurement"]["source"])

    def test_restating_a_measurement_corrects_it_over_the_route(self):
        first_status, first = self.measurement({"weight_kg": 72.5})
        second_status, second = self.measurement({"weight_kg": 72.3})

        self.assertEqual((200, 200), (first_status, second_status))
        self.assertEqual(first["measurement"], second["replaced"])
        self.assertEqual(1, second["measurement_count"])

    def test_an_activity_summary_is_stored_and_echoed_back_exactly_as_stored(self):
        status, payload = self.reported_activity(
            {
                "date": "2026-08-12",
                "sport": "running",
                "duration_minutes": 40,
                "distance_km": 6.4,
                "subjective_feel": 3,
                "note": "沒帶錶",
            }
        )

        self.assertEqual(200, status, payload)
        stored = athlete_evidence.load_evidence(self.state_dir)["reported_activities"][0]
        self.assertEqual(stored, payload["activity"])
        self.assertIsNone(payload["replaced_note"])

    def test_restating_an_activity_summary_corrects_it_and_names_the_displacement(self):
        _, first = self.reported_activity({"sport": "running", "duration_minutes": 40})
        _, second = self.reported_activity({"sport": "running", "duration_minutes": 45})

        self.assertEqual(first["activity"], second["replaced"])
        self.assertEqual(1, second["activity_count"])
        self.assertIn("combined summary", second["replaced_note"])

    # -- what the athlete is training for, and how they like to train (#164) -----------

    def test_a_long_term_goal_stated_once_answers_a_later_conversation(self):
        status, payload = self.long_term_goal(
            {"metric": "VO2max", "target": "50", "target_date": "2027-06-30"}
        )
        self.assertEqual(200, status, payload)
        self.assertIsNone(payload["replaced"])

        _, session = self.session()

        goals = session["context"]["long_term_goals"]["goals"]
        self.assertEqual([("VO2max", "50", "2027-06-30")],
                         [(g["metric"], g["target"], g["target_date"]) for g in goals])

    def test_a_stated_habit_answers_a_later_conversation_and_constrains_nothing(self):
        status, payload = self.training_preference(
            {"topic": "重訓頻率", "statement": "每週想重訓五次"}
        )
        self.assertEqual(200, status, payload)

        _, session = self.session()

        context = session["context"]
        self.assertEqual(
            "每週想重訓五次", context["training_preferences"]["preferences"][0]["statement"]
        )
        # A habit is not availability and not a red flag: it reaches the coach as
        # something to plan from, and nothing in constraints moved because of it.
        self.assertEqual([], context["constraints"]["week_constraints"])
        self.assertIsNone(context["constraints"]["availability_source"])

    def test_a_travel_week_constrains_this_week_and_leaves_the_habit_standing(self):
        self.training_preference({"topic": "長跑日", "statement": "習慣週日長跑"})
        self.availability({"recurring": {"available_days": ["mon", "wed", "fri", "sun"]}})

        status, payload = self.availability(
            {"week": {"unavailable_days": ["fri"], "note": "出差，只有飯店啞鈴"}}
        )
        self.assertEqual(200, status, payload)
        self.assertEqual(
            ["出差，只有飯店啞鈴"], payload["effective_this_week"]["week_constraints"]
        )

        _, session = self.session()
        context = session["context"]

        self.assertEqual(["出差，只有飯店啞鈴"], context["constraints"]["week_constraints"])
        self.assertNotIn("fri", context["constraints"]["available_days"])
        # The week that could not honour the habit did not edit the habit.
        self.assertEqual(
            "習慣週日長跑", context["training_preferences"]["preferences"][0]["statement"]
        )

    def test_restating_either_replaces_it_and_names_what_it_displaced(self):
        self.long_term_goal({"metric": "體重", "target": "80 kg"})
        _, goal = self.long_term_goal({"metric": "體重", "target": "78 kg"})
        self.assertEqual("80 kg", goal["replaced"]["target"])
        self.assertEqual(1, len(goal["long_term_goals"]))

        self.training_preference({"topic": "長跑日", "statement": "習慣週日長跑"})
        _, preference = self.training_preference({"topic": "長跑日", "statement": "改週六"})
        self.assertEqual("習慣週日長跑", preference["replaced"]["statement"])
        self.assertEqual(1, len(preference["training_preferences"]))

    def test_either_is_taken_back_through_the_one_retraction_route(self):
        self.long_term_goal({"metric": "VO2max", "target": "50"})
        self.training_preference({"topic": "重訓頻率", "statement": "每週想重訓五次"})

        status, dropped_goal = self.retract({"kind": "long_term_goal", "metric": "VO2max"})
        self.assertEqual(200, status, dropped_goal)
        self.assertEqual("50", dropped_goal["removed"]["target"])
        self.assertEqual(0, dropped_goal["record_count"])
        self.assertIsNone(dropped_goal["on_record_that_day"])

        status, dropped_habit = self.retract(
            {"kind": "training_preference", "topic": "重訓頻率"}
        )
        self.assertEqual(200, status, dropped_habit)
        self.assertEqual("每週想重訓五次", dropped_habit["removed"]["statement"])

        _, session = self.session()
        self.assertIsNone(session["context"]["long_term_goals"])
        self.assertIsNone(session["context"]["training_preferences"])

    def test_retracting_one_that_is_not_there_says_so_and_names_what_is(self):
        self.long_term_goal({"metric": "VO2max", "target": "50"})

        status, payload = self.retract({"kind": "long_term_goal", "metric": "體脂"})

        self.assertEqual(200, status, payload)
        self.assertTrue(payload["retracted"])
        self.assertIsNone(payload["removed"])
        self.assertIn("VO2max", payload["note"])

    def test_neither_route_touches_the_plan(self):
        _, before = self.session()
        self.long_term_goal({"metric": "VO2max", "target": "50"})
        self.training_preference({"topic": "重訓頻率", "statement": "每週想重訓五次"})

        _, after = self.session()

        # The cycle's own goal is the coach's milestone and moves only through a
        # decision; a long-term goal the athlete stated is not a route into it.
        self.assertEqual(before["plan_state"], after["plan_state"])

    def test_both_are_refused_when_malformed_and_store_nothing(self):
        cases = (
            ("long_term_goal_record", {}),
            ("long_term_goal_record", {"metric": "VO2max"}),
            ("long_term_goal_record", {"target": "50"}),
            ("long_term_goal_record", {"metric": "VO2max", "target": ""}),
            (
                "long_term_goal_record",
                {"metric": "5K", "target": "sub-25", "target_date": "next June"},
            ),
            # A current value is not a goal, and there is no field that would take one.
            (
                "long_term_goal_record",
                {"metric": "VO2max", "target": "50", "current": "46"},
            ),
            ("training_preference_record", {}),
            ("training_preference_record", {"topic": "重訓頻率"}),
            ("training_preference_record", {"statement": "每週想重訓五次"}),
            (
                "training_preference_record",
                {"topic": "重訓頻率", "statement": "五次", "sessions_per_week": 5},
            ),
        )
        for kind, body in cases:
            with self.subTest(kind=kind, body=body):
                status, payload = self.route(kind, body=body, token=TOKEN_A)
                self.assertEqual(400, status, payload)
        evidence = athlete_evidence.load_evidence(self.state_dir)
        self.assertEqual([], evidence["long_term_goals"])
        self.assertEqual([], evidence["training_preferences"])

    def test_a_retraction_of_a_goal_cannot_also_restate_one(self):
        status, payload = self.retract(
            {"kind": "long_term_goal", "metric": "VO2max", "target": "52"}
        )
        self.assertEqual(400, status, payload)

    def test_the_two_new_statements_are_refused_when_malformed_and_store_nothing(self):
        cases = (
            ("body_measurement_record", {}),
            ("body_measurement_record", {"weight_kg": 7.2}),
            ("body_measurement_record", {"body_fat_pct": 90}),
            ("body_measurement_record", {"weight_kg": 72.5, "wieght_kg": 72.5}),
            ("body_measurement_record", {"weight_kg": 72.5, "date": "2026-08-20"}),
            ("activity_summary_record", {"sport": "running"}),
            ("activity_summary_record", {"duration_minutes": 40}),
            ("activity_summary_record", {"sport": "climbing", "duration_minutes": 40}),
            ("activity_summary_record", {"sport": "rest", "duration_minutes": 40}),
            (
                "activity_summary_record",
                {"sport": "running", "duration_minutes": 40, "subjective_feel": 9},
            ),
            (
                "activity_summary_record",
                {"sport": "running", "duration_minutes": 40, "date": "2026-08-20"},
            ),
        )
        for kind, body in cases:
            with self.subTest(kind=kind, body=body):
                status, payload = self.route(kind, body=body, token=TOKEN_A)
                self.assertEqual(400, status, payload)
                self.assertEqual("invalid_request", payload["error"])
        self.assertFalse((self.state_dir / "athlete-evidence.json").exists())

    def test_neither_new_route_answers_without_a_token(self):
        for kind in ("body_measurement_record", "activity_summary_record"):
            with self.subTest(kind=kind):
                status, payload = self.route(kind, body={}, token=None)
                self.assertEqual(401, status)
                self.assertEqual("unauthorized", payload["error"])
        self.assertFalse((self.state_dir / "athlete-evidence.json").exists())

    def test_both_new_groups_reach_the_next_conversations_context_labelled(self):
        self.measurement({"date": "2026-08-11", "weight_kg": 73.0})
        self.measurement({"weight_kg": 72.5, "body_fat_pct": 18.4})
        self.reported_activity(
            {"date": "2026-08-12", "sport": "running", "duration_minutes": 40, "distance_km": 6.4}
        )

        context = self.session()[1]["context"]

        measurements = context["body_measurements"]
        self.assertEqual("athlete_reported", measurements["source"])
        self.assertEqual(
            ["2026-08-13", "2026-08-11"], [row["date"] for row in measurements["measurements"]]
        )
        activities = context["reported_activities"]
        self.assertEqual("athlete_reported", activities["source"])
        self.assertEqual(1, len(activities["activities"]))
        self.assertEqual("athlete_reported", activities["activities"][0]["source"])
        # No provider activity anywhere near it, and the row says so explicitly --
        # False is "checked, nothing there", never an absent key.
        self.assertIs(False, activities["activities"][0]["provider_actual_same_day"])

    def test_three_weeks_of_saying_it_reads_as_a_pattern_in_the_next_conversation(self):
        """Issue #188's whole point: the run the coach could never see before.

        Each statement used to survive only as the plan change it caused, so a third
        consecutive week of "very tired" arrived looking exactly like a first. Here the
        notes come back with their dates, in the athlete's own words, and the reading of
        them is the coach's -- nothing here counts a streak or scores a day.
        """
        for day, note in (
            ("2026-08-13", "還是很累"),
            ("2026-08-06", "這週也很累"),
            ("2026-08-01", "累了一整週"),
        ):
            status, _ = self.subjective_state({"date": day, "note": note})
            self.assertEqual(200, status)

        context = self.session()[1]["context"]

        states = context["subjective_states"]
        self.assertEqual("athlete_reported", states["source"])
        self.assertEqual(
            ["2026-08-13", "2026-08-06", "2026-08-01"],
            [row["date"] for row in states["states"]],
        )
        self.assertEqual("累了一整週", states["states"][-1]["note"])
        # Rows and dates, and nothing derived from them: no streak, no severity, no
        # comparison against the recovery evidence sitting beside them in this context.
        self.assertEqual({"date", "note", "recorded_at"}, set(states["states"][0]))

    def test_the_group_carries_the_fortnight_it_was_actually_read_over(self):
        """A shorter window than the rest of this file, stated rather than assumed.

        A group that named the 42-day span while holding only fourteen days of rows would
        tell the coach nothing was said in a month nobody looked at.
        """
        self.subjective_state({"date": "2026-08-13", "note": "很累"})
        self.subjective_state({"date": "2026-07-31", "note": "邊界內"})
        self.subjective_state({"date": "2026-07-30", "note": "太舊了"})

        states = self.session()[1]["context"]["subjective_states"]

        self.assertEqual("2026-07-31", states["window_start"])
        self.assertEqual("2026-08-13", states["window_end"])
        self.assertEqual(
            ["2026-08-13", "2026-07-31"], [row["date"] for row in states["states"]]
        )

    def test_an_athlete_who_has_said_nothing_has_a_null_group_rather_than_an_empty_one(self):
        """The ordinary starting state, and the one the coach may ask about."""
        self.assertIsNone(self.session()[1]["context"]["subjective_states"])

    def test_a_stored_note_changes_nothing_else_about_the_context(self):
        """It is evidence, not a trigger: no rule fires, no session moves, nothing scores.

        Checked the way a reported session is -- against a second account in the same
        state -- so the claim is that the *whole* context is byte-identical apart from the
        group the note lives in.
        """
        self.seed_owner(TOKEN_B, athlete_id="i2", plan=publishable_plan())
        self.subjective_state({"note": "累到不行，完全爬不起來"})

        _, stated = self.session()
        _, plain = self.session(token=TOKEN_B)

        self.assertEqual(plain["reconciliation"], stated["reconciliation"])
        self.assertEqual(
            {"subjective_states"},
            {
                key
                for key in plain["context"]
                if plain["context"][key] != stated["context"][key]
            },
        )

    def test_a_subjective_state_retracts_by_the_day_it_was_made_against(self):
        self.subjective_state({"date": "2026-08-12", "note": "前一天"})
        self.subjective_state({"note": "今天"})

        status, payload = self.retract({"kind": "subjective_state", "date": "2026-08-12"})

        self.assertEqual(200, status, payload)
        self.assertTrue(payload["retracted"])
        self.assertEqual("前一天", payload["removed"]["note"])
        self.assertEqual(1, payload["record_count"])
        states = self.session()[1]["context"]["subjective_states"]
        self.assertEqual(["2026-08-13"], [row["date"] for row in states["states"]])

    def test_the_route_refuses_a_statement_the_athlete_cannot_have_made(self):
        for body in (
            {"note": ""},
            {"note": "很累", "date": "2026-08-20"},
            {"note": "很累", "mood": 2},
        ):
            with self.subTest(body=body):
                status, payload = self.subjective_state(body)
                self.assertEqual(400, status, payload)
                self.assertEqual("invalid_request", payload["error"])

    def test_a_reported_session_is_never_read_as_a_provider_actual(self):
        """Issue #140's central claim, checked against the reconciliation output itself.

        A report is deliberately shaped so that it *cannot* attach: no activity id, no
        match confidence, nothing offered to the matcher. The proof is that a second
        account with identical state produces a byte-identical context apart from the
        group the report lives in -- so the report moved no actual, no coverage row, no
        cycle-session evidence state, and nothing reconciliation reads.

        A report that leaked into `recent_actuals` would be the worse failure of the two
        it could produce: a week of training counted twice, once as the athlete's word and
        once as the provider's.
        """
        self.seed_owner(TOKEN_B, athlete_id="i2", plan=publishable_plan())
        # A real provider activity, so the comparison is between two accounts that both
        # have actuals to reconcile rather than two empty ones.
        self.fake.activities = [
            {
                "id": "i4001",
                "type": "Run",
                "start_date_local": "2026-08-11T07:00:00",
                "moving_time": 2400,
                "distance": 8000.0,
                "average_speed": 3.33,
                "average_heartrate": 148,
            }
        ]
        # Aimed squarely at a planned session -- same day, same sport -- so anything that
        # could match it would.
        self.reported_activity(
            {"date": "2026-08-13", "sport": "running", "duration_minutes": 50, "distance_km": 9.0}
        )
        self.measurement({"weight_kg": 72.5})

        _, reported = self.session()
        _, plain = self.session(token=TOKEN_B)

        self.assertEqual(plain["reconciliation"], reported["reconciliation"])
        # One actual, the provider's, and no second row for the session the athlete
        # reported on the 13th.
        self.assertEqual(
            ["intervals:i4001"],
            [item["activity_id"] for item in reported["context"]["recent_actuals"]],
        )
        # training_history, evidence_expectations and unknowns now differ too, and
        # correctly: the reported session is also the one row training_history's
        # unwindowed rollup has to show; it and the measurement are two streams the
        # reported account has evidence in and the plain one does not (issue #28); and
        # the plain account's unknowns carries the note training_history's own absence
        # adds (issue #101) that the reported account's non-null group does not. None of
        # them is the reconciliation leak this test exists to catch -- see below.
        self.assertEqual(
            {
                "body_measurements",
                "reported_activities",
                "training_history",
                "evidence_expectations",
                "unknowns",
            },
            {
                key
                for key in plain["context"]
                if plain["context"][key] != reported["context"][key]
            },
        )
        self.assertIsNone(plain["context"]["reported_activities"])
        # The provider actual is two days earlier, so the report claims no overlap.
        self.assertIs(
            False,
            reported["context"]["reported_activities"]["activities"][0][
                "provider_actual_same_day"
            ],
        )
        # And the plan itself did not move: a report commits nothing.
        self.assertEqual(
            plain["plan_state"]["plan_version"], reported["plan_state"]["plan_version"]
        )

    def test_a_report_names_when_the_provider_also_holds_its_day_and_sport(self):
        """The late-sync case, stated instead of double-counted.

        The usual life of a report: the watch failed, the athlete said the numbers --
        and then the watch synced after all. Nothing merges and nothing hides (the
        reconciliation-identity test above still holds byte for byte); the reported row
        simply states that the provider also holds an activity of its sport on its day,
        and whether the two are one session is the coach's reading.
        """
        self.fake.activities = [
            {
                "id": "i4002",
                "type": "Run",
                "start_date_local": "2026-08-11T07:00:00",
                "moving_time": 2400,
                "distance": 8000.0,
                "average_speed": 3.33,
                "average_heartrate": 148,
            }
        ]
        self.reported_activity(
            {"date": "2026-08-11", "sport": "running", "duration_minutes": 40}
        )
        self.reported_activity(
            {"date": "2026-08-11", "sport": "swimming", "duration_minutes": 30}
        )

        context = self.session()[1]["context"]

        rows = {row["sport"]: row for row in context["reported_activities"]["activities"]}
        self.assertIs(True, rows["running"]["provider_actual_same_day"])
        # Same day, different sport: the swim stays unflagged -- sport is part of the
        # identity, not a nicety.
        self.assertIs(False, rows["swimming"]["provider_actual_same_day"])

    # -- retraction: one shared route, `kind` picks the record family ------------------

    def test_all_three_kinds_remove_the_record_over_the_route(self):
        """Correcting replaces a record; retracting removes it -- one route, three kinds."""
        self.strength(
            {
                "date": "2026-08-12",
                "exercise": "bench press",
                "category": "chest",
                "sets": [{"set": 1, "weight_kg": 65, "reps": 4}],
            }
        )
        self.measurement({"date": "2026-08-12", "weight_kg": 72.5})
        self.reported_activity(
            {"date": "2026-08-12", "sport": "running", "duration_minutes": 40}
        )

        strength_status, strength_payload = self.retract(
            {"kind": "strength_execution", "date": "2026-08-12", "exercise": "bench press"}
        )
        measurement_status, measurement_payload = self.retract(
            {"kind": "body_measurement", "date": "2026-08-12"}
        )
        activity_status, activity_payload = self.retract(
            {"kind": "activity_summary", "date": "2026-08-12", "sport": "running"}
        )

        self.assertEqual(
            (200, 200, 200), (strength_status, measurement_status, activity_status)
        )
        for payload in (strength_payload, measurement_payload, activity_payload):
            self.assertEqual("passed", payload["status"])
            self.assertTrue(payload["retracted"])
            self.assertIsNotNone(payload["removed"])
            self.assertIsNone(payload["note"])
            self.assertEqual(0, payload["record_count"])
        # Keyed by date alone -- no second name it could have gotten wrong.
        self.assertIsNone(measurement_payload["on_record_that_day"])

        # No tombstone, no empty session left: the store file no longer holds any of
        # the three records at all.
        stored = athlete_evidence.load_evidence(self.state_dir)
        self.assertEqual([], stored["strength_reports"])
        self.assertEqual([], stored["body_measurements"])
        self.assertEqual([], stored["reported_activities"])

    def test_retracting_something_not_on_record_is_a_plain_no_op_not_an_error(self):
        status, payload = self.retract(
            {"kind": "strength_execution", "exercise": "bench press"}
        )

        self.assertEqual(200, status, payload)
        self.assertEqual("passed", payload["status"])
        self.assertTrue(payload["retracted"])
        self.assertIsNone(payload["removed"])
        self.assertEqual([], payload["on_record_that_day"])
        self.assertIsNotNone(payload["note"])
        # Never even created: an athlete who reported nothing and retracts a lift they
        # never described has not caused a write.
        self.assertFalse((self.state_dir / "athlete-evidence.json").exists())

    def test_a_field_not_valid_for_the_given_kind_is_refused(self):
        """_only_fields per kind: the record's own content, and another kind's field."""
        cases = (
            {"kind": "strength_execution", "exercise": "bench press", "sets": [{"reps": 4}]},
            {"kind": "strength_execution", "exercise": "bench press", "sport": "running"},
            {"kind": "body_measurement", "weight_kg": 72.5},
            {"kind": "body_measurement", "exercise": "bench press"},
            {"kind": "activity_summary", "sport": "running", "duration_minutes": 40},
            {"kind": "activity_summary", "sport": "running", "exercise": "bench press"},
        )
        for body in cases:
            with self.subTest(body=body):
                status, payload = self.retract(body)
                self.assertEqual(400, status, payload)
                self.assertEqual("invalid_request", payload["error"])
        self.assertFalse((self.state_dir / "athlete-evidence.json").exists())

    def test_an_unrecognized_kind_is_refused(self):
        status, payload = self.retract({"kind": "banana"})

        self.assertEqual(400, status, payload)
        self.assertEqual("invalid_request", payload["error"])


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

    def say(self, kind: str, body: dict[str, Any]) -> dict[str, Any]:
        """One thing the athlete said, as one call. Counts, so a test can assert the cost."""
        self.calls += 1
        status, payload = self.route(kind, body=body, token=TOKEN_A)
        self.assertEqual(200, status, payload)
        return payload

    def availability(self, body: dict[str, Any]) -> dict[str, Any]:
        return self.say("availability_record", body)

    def strength(self, body: dict[str, Any]) -> dict[str, Any]:
        return self.say("strength_report", body)

    def constraints(self) -> dict[str, Any]:
        status, payload = self.route("session", body={}, token=TOKEN_A)
        self.assertEqual(200, payload and status, payload)
        return payload["context"]["constraints"]

    def sessions(self) -> list[dict[str, Any]]:
        _, payload = self.route("session", body={}, token=TOKEN_A)
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
        status, payload = self.route("session", body={}, token=TOKEN_A)

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

    def test_client_uploaded_recovery_is_available_when_building_the_first_plan(self):
        status, payload = self.route(
            "session",
            body={"recovery_signals": recovery_signals_upload()},
            token=TOKEN_A,
        )

        self.assertEqual(200, status, payload)
        self.assertEqual("no_plan_state", payload["status"])
        group = payload["pre_plan_observations"]["recovery_signals"]
        self.assertEqual(
            "client-uploaded:personal-os:recovery_daily+daily_metrics", group["source"]
        )
        self.assertEqual("2026-08-13", group["days"][0]["date"])
        # No PlanState or recovery copy was created by a request-scoped upload.
        self.assertFalse(self.state_dir.exists())

    def test_an_athlete_with_no_plan_yet_gets_the_training_judgment_too(self):
        """The turn that authors the first 28 days is the one that needs it most."""
        status, payload = self.route("session", body={}, token=TOKEN_A)

        self.assertEqual(200, status, payload)
        self.assertEqual("no_plan_state", payload["status"])
        self.assertEqual(orchestration.training_judgment(), payload["coaching_guidance"])

    def test_a_freshly_registered_intervals_account_is_guided_to_its_first_evidence(self):
        """Issue #225: the biggest controllable break in the new-athlete funnel was
        silence -- nothing told the athlete Intervals itself was empty, or what to do
        about it, so whether it got said at all depended on that conversation's model.
        """
        self.fake.activities = []

        status, payload = self.route("session", body={}, token=TOKEN_A)

        self.assertEqual(200, status, payload)
        observations = payload["pre_plan_observations"]
        self.assertIsNone(observations["athlete_evidence"])
        self.assertEqual([], observations["recent_training"]["recent_actuals"])
        guidance = payload["coaching_guidance"]
        # An addition to the training judgment, never a replacement of it.
        self.assertIn(orchestration.training_judgment(), guidance)
        self.assertNotEqual(orchestration.training_judgment(), guidance)
        # The three elements the guide owes them: where to go, what happens there,
        # and what they get back for it.
        self.assertIn("Intervals", guidance)
        self.assertIn("backfills", guidance)
        self.assertIn("startCoachSession", guidance)
        # The upload alternative, named by the tool call it actually reaches.
        self.assertIn("importAthleteHistory", guidance)
        self.assertIn("CSV", guidance)

    def test_self_reported_activity_evidence_also_counts_as_not_empty(self):
        """A session logged by hand is activity evidence too, Intervals empty or not."""
        self.fake.activities = []
        self.route(
            "activity_summary_record",
            body={"date": "2026-08-12", "sport": "running", "duration_minutes": 40},
            token=TOKEN_A,
        )

        _, payload = self.route("session", body={}, token=TOKEN_A)

        self.assertEqual(orchestration.training_judgment(), payload["coaching_guidance"])

    def test_a_stated_goal_with_no_activity_evidence_still_gets_guided(self):
        """A goal is not activity evidence -- the coach still has nothing to read cold."""
        self.fake.activities = []
        self.route(
            "long_term_goal_record",
            body={"metric": "體重", "target": "80 kg"},
            token=TOKEN_A,
        )

        _, payload = self.route("session", body={}, token=TOKEN_A)

        self.assertIn("importAthleteHistory", payload["coaching_guidance"])

    def test_a_provider_read_failure_is_not_guessed_as_an_empty_account(self):
        """A failed read is unknown, never "empty" -- AGENTS.md 3 cuts both ways."""
        self.fake.activities = []
        self.fake.read_status = 500

        status, payload = self.route("session", body={}, token=TOKEN_A)

        self.assertEqual(200, status)
        self.assertIsNone(payload["pre_plan_observations"]["recent_training"])
        self.assertEqual(orchestration.training_judgment(), payload["coaching_guidance"])

    def test_a_provider_that_cannot_be_read_lowers_the_answer_without_blocking_it(self):
        self.fake.read_status = 500
        self.route(
            "activity_summary_record",
            body={"date": "2026-08-11", "sport": "running", "duration_minutes": 30},
            token=TOKEN_A,
        )

        status, payload = self.route("session", body={}, token=TOKEN_A)

        self.assertEqual(200, status)
        self.assertEqual("no_plan_state", payload["status"])
        self.assertIsNone(payload["pre_plan_observations"]["recent_training"])
        self.assertIn("no PlanState exists for this account", payload["unknowns"])
        self.assertTrue(
            [note for note in payload["unknowns"] if note.startswith("recent_training unavailable")],
            payload["unknowns"],
        )
        # No provider read happened, so no row claims "checked, nothing there": the
        # overlap flag is absent, not False.
        report = payload["pre_plan_observations"]["athlete_evidence"]["reported_activities"][0]
        self.assertNotIn("provider_actual_same_day", report)

    def test_a_goal_stated_before_any_plan_is_read_back_before_asking(self):
        """The likely first sentence, and the one initialization must not re-ask for.

        "I want to get to 80 kg" arrives before there is any plan to hold a cycle goal --
        and the cycle goal the coach is about to write is a milestone toward it, not a
        replacement for it (issue #164).
        """
        for kind, body in (
            ("long_term_goal_record", {"metric": "體重", "target": "80 kg"}),
            (
                "training_preference_record",
                {"topic": "重訓頻率", "statement": "每週想重訓五次"},
            ),
        ):
            self.route(kind, body=body, token=TOKEN_A)

        _, payload = self.route("session", body={}, token=TOKEN_A)

        self.assertEqual("no_plan_state", payload["status"])
        evidence = payload["pre_plan_observations"]["athlete_evidence"]
        self.assertEqual("80 kg", evidence["long_term_goals"][0]["target"])
        self.assertEqual(
            "每週想重訓五次", evidence["training_preferences"][0]["statement"]
        )

    def test_availability_reported_before_any_plan_is_read_back_before_asking(self):
        self.route(
            "availability_record",
            body={"recurring": {"available_days": ["mon", "wed", "fri"]}},
            token=TOKEN_A,
        )

        _, payload = self.route("session", body={}, token=TOKEN_A)

        evidence = payload["pre_plan_observations"]["athlete_evidence"]
        self.assertEqual(["mon", "wed", "fri"], evidence["availability"]["recurring"]["available_days"])
        self.assertEqual(
            ["mon", "wed", "fri"], evidence["availability"]["effective_this_week"]["available_days"]
        )
        self.assertEqual([], evidence["strength_reports"])

    def test_lifts_reported_before_any_plan_arrive_whole_not_as_a_count(self):
        self.route(
            "strength_report",
            body={
                "date": "2026-08-12",
                "exercise": "bench press",
                "category": "chest",
                "sets": [{"set": 1, "weight_kg": 65, "reps": 4}],
            },
            token=TOKEN_A,
        )

        _, payload = self.route("session", body={}, token=TOKEN_A)

        reports = payload["pre_plan_observations"]["athlete_evidence"]["strength_reports"]
        self.assertEqual(1, len(reports))
        self.assertEqual(65, reports[0]["sets"][0]["weight_kg"])

    def test_a_weight_or_session_stated_before_any_plan_is_read_back_too(self):
        """The same rule as availability and lifts, for the same reason.

        An athlete who says "我 72.5 公斤" or "昨天游了 40 分鐘" in the first conversation has
        already answered a question the first plan would otherwise ask them.
        """
        self.route("body_measurement_record", body={"weight_kg": 72.5}, token=TOKEN_A)
        self.route(
            "activity_summary_record",
            body={"date": "2026-08-12", "sport": "running", "duration_minutes": 40},
            token=TOKEN_A,
        )

        _, payload = self.route("session", body={}, token=TOKEN_A)

        evidence = payload["pre_plan_observations"]["athlete_evidence"]
        self.assertEqual(72.5, evidence["body_measurements"][0]["weight_kg"])
        self.assertEqual(40, evidence["reported_activities"][0]["duration_minutes"])
        # The provider read succeeded and holds no 08-12 activity, and the row says so
        # -- the same flag a full context writes, because this response too puts the
        # athlete's word and the provider's actuals side by side.
        self.assertIs(False, evidence["reported_activities"][0]["provider_actual_same_day"])

    def test_a_pre_plan_report_names_when_the_provider_also_holds_its_day(self):
        """The late-sync case, in the one conversation likeliest to hit it.

        A first conversation is exactly where a watch-failed report and a late-synced
        activity coexist -- the athlete reported because nothing recorded, then the watch
        synced after all. The full-context path already states the overlap; this pins the
        no-plan path to the same statement, against the Run actual the provider holds on
        2026-08-11.
        """
        self.route(
            "activity_summary_record",
            body={"date": "2026-08-11", "sport": "running", "duration_minutes": 30},
            token=TOKEN_A,
        )
        self.route(
            "activity_summary_record",
            body={"date": "2026-08-11", "sport": "swimming", "duration_minutes": 30},
            token=TOKEN_A,
        )

        _, payload = self.route("session", body={}, token=TOKEN_A)

        rows = {
            row["sport"]: row
            for row in payload["pre_plan_observations"]["athlete_evidence"]["reported_activities"]
        }
        self.assertIs(True, rows["running"]["provider_actual_same_day"])
        self.assertIs(False, rows["swimming"]["provider_actual_same_day"])

    def test_an_account_that_already_has_a_plan_carries_no_such_field(self):
        self.seed_owner(TOKEN_B, athlete_id="i2", plan=publishable_plan())

        _, payload = self.route("session", body={}, token=TOKEN_B)

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
        status, payload = self.route("session", body={}, token=TOKEN_A)
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
        status, prepared = self.route(
            "decision_prepare", body=body, token=TOKEN_A
        )
        self.assertEqual(200, status, prepared)
        status, applied = self.route(
            "decision_apply",
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
        status, prepared = self.route(
            "delivery_prepare",
            body={
                "plan_id": current["plan_state"]["plan_id"],
                "plan_version": current["plan_state"]["plan_version"],
                "session_ids": session_ids,
            },
            token=TOKEN_A,
        )
        self.assertEqual(200, status, prepared)
        status, published = self.route(
            "delivery_apply",
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
        status, prepared = self.route(
            "delivery_prepare",
            body={
                "plan_id": current["plan_state"]["plan_id"],
                "plan_version": current["plan_state"]["plan_version"],
                "session_ids": ["run-quality-01"],
                "withdraw": True,
            },
            token=TOKEN_A,
        )
        self.assertEqual(200, status, prepared)
        status, withdrawn = self.route(
            "delivery_apply",
            body={
                "delivery_set": prepared["delivery_set"],
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

    def test_a_coach_note_reaches_the_calendar_and_re_delivers_when_it_moves(self):
        """Issue #56, over the transport an athlete actually uses.

        The sentence has to survive every hop it is worth having: the confirmation preview,
        the provider write, the read-back that decides whether the delivery may be reported
        at all -- and then a reword has to take the calendar entry with it, or the athlete
        keeps reading advice the plan has already withdrawn.
        """
        note = "這週的長跑故意排短，是為了下週的測試——不要自己加量"
        self.decide(
            {
                "summary": "替這堂課補一句說明",
                "reason_codes": ["plan_kept_no_material_change"],
                "evidence": [{"field": "current_calendar", "observation": "本週長跑刻意縮短"}],
                "goal_effect": {"week": "訓練不動，只是講清楚為什麼", "cycle": "28 天方向不變"},
                "next_review_condition": "下週測試後重新評估",
                "sessions": [
                    {"operation": "keep", "session_id": "run-quality-01", "coach_note": note}
                ],
            }
        )

        current = self.session()
        status, prepared = self.route(
            "delivery_prepare",
            body={
                "plan_id": current["plan_state"]["plan_id"],
                "plan_version": current["plan_state"]["plan_version"],
                "session_ids": ["run-quality-01"],
            },
            token=TOKEN_A,
        )
        self.assertEqual(200, status, prepared)
        # The athlete confirms an exact preview, and the sentence is in it.
        self.assertIn(note, prepared["preview"][0]["delivered_description"])

        status, published = self.route(
            "delivery_apply",
            body={
                "delivery_set": prepared["delivery_set"],
                "proposal_hash": prepared["proposal_hash"],
                "confirmed": True,
            },
            token=TOKEN_A,
        )
        self.assertEqual(200, status, published)
        self.assertEqual("intervals_accepted", published["delivery_state"])
        # What the calendar holds, read off the provider rather than off the plan.
        self.assertIn(note, self.fake.events[0]["description"])

        # A reworded note is a different thing to have told the athlete, so the delivered
        # entry no longer describes the plan and has to be sent again.
        self.decide(
            {
                "summary": "把說明講得更清楚",
                "reason_codes": ["plan_kept_no_material_change"],
                "evidence": [{"field": "current_calendar", "observation": "說明語氣不夠明確"}],
                "goal_effect": {"week": "訓練不動", "cycle": "28 天方向不變"},
                "next_review_condition": "下週測試後重新評估",
                "sessions": [
                    {
                        "operation": "keep",
                        "session_id": "run-quality-01",
                        "coach_note": "這週故意排短，下週要測試，不要自己加量",
                    }
                ],
            }
        )

        stale = self.delivery_view("run-quality-01")
        self.assertEqual("not_published", stale["delivery_state"])
        self.assertIsNotNone(stale["superseded_external_id"])

    def test_a_set_that_fails_halfway_is_recoverable_without_writing_anything_twice(self):
        current = self.session()
        status, prepared = self.route(
            "delivery_prepare",
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

        status, published = self.route(
            "delivery_apply",
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
        status, refused = self.route(
            "delivery_prepare",
            body={
                "plan_id": self.session()["plan_state"]["plan_id"],
                "plan_version": self.session()["plan_state"]["plan_version"],
                "session_ids": [second_session],
            },
            token=TOKEN_A,
        )
        self.assertEqual(200, status, refused)
        status, blocked = self.route(
            "delivery_apply",
            body={
                "delivery_set": refused["delivery_set"],
                "proposal_hash": refused["proposal_hash"],
                "confirmed": True,
            },
            token=TOKEN_A,
        )
        self.assertEqual(409, status, blocked)

        self.fake.corrupt_external_ids.discard(second_owned_id)
        status, retried = self.route(
            "delivery_apply",
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
        self.fake.sport_settings = copy.deepcopy(RUN_SPORT_SETTINGS)

    # -- helpers ----------------------------------------------------------------------

    def session(
        self, *, token: str = TOKEN_A, body: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        status, payload = self.route(
            "session", body=body or {}, token=token
        )
        self.assertEqual(200, status, payload)
        return payload

    def interrupt(self, *, token: str = TOKEN_A) -> str:
        """Leave the store exactly as a delivery killed after one provider write does.

        The first session lands and verifies; the second is accepted by Intervals and
        reads back as something else, so its effect is real and unreconciled. That is the
        state the reservation exists to describe.
        """
        current = self.session(token=token)
        status, prepared = self.route(
            "delivery_prepare",
            body={
                "plan_id": current["plan_state"]["plan_id"],
                "plan_version": current["plan_state"]["plan_version"],
                "session_ids": ["run-quality-01", "run-long-01"],
            },
            token=token,
        )
        self.assertEqual(200, status, prepared)
        self.fake.corrupt_external_ids.add(prepared["preview"][1]["owned_external_id"])
        status, published = self.route(
            "delivery_apply",
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
        return self.route(
            "delivery_attempt_clear", body=body, token=token
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
        resumed = self.session(
            body={"recovery_signals": recovery_signals_upload()}
        )
        self.assertEqual("passed", resumed["reconciliation"]["status"])
        self.assertEqual(
            ["run-quality-01"],
            [item["session_id"] for item in resumed["reconciliation"]["applied"]],
        )
        self.assertIsNone(resumed["delivery"]["unresolved_delivery"])
        self.assertEqual(
            "client-uploaded:personal-os:recovery_daily+daily_metrics",
            resumed["context"]["recovery_signals"]["source"],
        )

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
            status, payload = self.route(
                "delivery_attempt_clear", body=body, token=TOKEN_A
            )
            self.assertEqual(409, status, payload)
            self.assertEqual("confirmation_required", payload["error"], confirmation)

        status, payload = self.route(
            "delivery_attempt_clear",
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
        status, prepared = self.route(
            "decision_prepare", body=body, token=TOKEN_A
        )
        self.assertEqual(200, status, prepared)
        status, refused = self.route(
            "decision_apply",
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
        status, applied = self.route(
            "decision_apply",
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
        "outlook": [
            {
                "week_start": "2026-08-17",
                "intent": "先把量拉起來，強度不動",
                "key_sessions": ["一次品質跑", "一次長的輕鬆跑", "兩次重訓"],
                "relation_to_primary": "推進主要適應",
            },
            {
                "week_start": "2026-08-24",
                "intent": "維持同樣的形狀，讓身體吸收",
                "key_sessions": ["一次品質跑", "一次長的輕鬆跑", "兩次重訓"],
                "relation_to_primary": "維持主要適應",
            },
            {
                "week_start": "2026-08-31",
                "intent": "量降下來，做這個週期自己的測量",
                "key_sessions": ["一次品質跑", "一次長的輕鬆跑", "兩次重訓"],
                "relation_to_primary": "量測主要適應",
            },
        ],
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
        status, payload = self.route("session", body={}, token=token)
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
        status, prepared = self.route(
            "decision_prepare", body=body, token=token
        )
        self.assertEqual(200, status, prepared)
        status, applied = self.route(
            "decision_apply",
            body={**body, "proposal": prepared["proposal"], "confirmed": True},
            token=token,
        )
        self.assertEqual(200, status, applied)
        return applied

    def deliver_for(self, token: str, session_ids: list[str]) -> dict[str, Any]:
        current = self.session_for(token)
        status, prepared = self.route(
            "delivery_prepare",
            body={
                "plan_id": current["plan_state"]["plan_id"],
                "plan_version": current["plan_state"]["plan_version"],
                "session_ids": session_ids,
            },
            token=token,
        )
        self.assertEqual(200, status, prepared)
        status, published = self.route(
            "delivery_apply",
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
        redeemed = self.gateway._redeem_intervals_code("second-athlete-c1")
        self.assertEqual(TOKEN_B, redeemed["access_token"])

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
        status, prepared_b = self.route(
            "decision_prepare",
            body={"change_request": as_change_request(SECOND_ATHLETE_ONBOARDING)},
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

        status, applied_b_init = self.route(
            "decision_apply",
            body={
                "change_request": as_change_request(SECOND_ATHLETE_ONBOARDING),
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
        # disk (not through the session route, which is free to reconcile fresh evidence
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

        # 7. A fresh conversation for B -- a new session call, exactly what
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


# --------------------------------------------------------------------------------------
# Provider request budget
# --------------------------------------------------------------------------------------


class GatewayProviderRequestBudgetTests(GatewayTestCase):
    """Exactly which provider requests one ``startCoachSession`` is allowed to make.

    Every other test here asserts what a response *says*; these assert what reaching it
    cost. The counts are exact rather than upper bounds because both failures this
    guards against are silent: a read whose answer nothing consumes shows up nowhere in
    any response, and a second read of the same endpoint inside one request produces the
    same answer as the first right up until the athlete's account moves between them,
    at which point one response describes two different moments.
    """

    def provider_gets(self) -> list[str]:
        """Every provider GET this test made, by endpoint, in the order it was issued.

        Trimmed of the athlete-scoped prefix so the list reads as endpoints; the
        per-activity segment read hangs off an activity rather than the athlete, so it
        keeps its own shape and its activity id with it.
        """
        endpoints = []
        for method, url in self.fake.calls:
            if method != "GET" or url.startswith(INTERVALS_TOKEN_URL):
                continue
            path = urllib.parse.urlsplit(url).path
            endpoints.append(
                path.removeprefix("/api/v1/athlete/0").removeprefix("/api/v1")
            )
        return endpoints

    def test_an_account_with_no_store_reads_activities_and_nothing_else(self):
        self.seed_owner(TOKEN_A)

        status, payload = self.route("session", body={}, token=TOKEN_A)

        self.assertEqual(200, status, payload)
        self.assertEqual("no_plan_state", payload["status"])
        # One request, and it is the training history the first conversation exists to
        # avoid re-asking for. Wellness has no reader on this path and the Run sport
        # settings have no baseline max HR to disagree with, so neither is requested.
        self.assertEqual(["/activities"], self.provider_gets())

    def test_a_wellness_outage_no_longer_costs_a_new_athlete_their_history(self):
        """The visible behaviour change: recovery and history stop sharing a fate.

        Before, the pre-plan view read a whole context domain, so a wellness endpoint
        that was down took ``recent_training`` down with it -- on the one turn where the
        history is the entire reason for asking.
        """
        self.seed_owner(TOKEN_A)
        self.fake.activities = [
            {
                "id": "i9001",
                "type": "Run",
                "start_date_local": "2026-08-11T07:00:00",
                "moving_time": 2100,
                "distance": 7000.0,
                "average_speed": 3.33,
            }
        ]

        def wellness_is_down(request: urllib.request.Request) -> bytes:
            if "/wellness?" in request.full_url:
                raise _http_error(request.full_url, 500)
            return FakeIntervals.__call__(self.fake, request)

        self.gateway.fetch = wellness_is_down
        status, payload = self.route("session", body={}, token=TOKEN_A)

        self.assertEqual(200, status, payload)
        observations = payload["pre_plan_observations"]
        self.assertIsNotNone(observations["recent_training"])
        self.assertEqual(
            ["intervals:i9001"],
            [item["activity_id"] for item in observations["recent_training"]["recent_actuals"]],
        )

    def test_a_wellness_outage_no_longer_costs_an_athlete_with_a_plan_their_turn(self):
        """The other half of the pair above, and the one an athlete feels.

        The account that has a plan is the one asking what to do today. A wellness
        endpoint that is down used to answer that with ``502 provider_error`` -- no
        context, no plan state, no session -- while the same outage cost an account
        with no plan nothing at all. Now both turns answer, and this one says its
        recovery half was not read rather than reporting zero observed days as if a
        provider had reported them.
        """
        self.seed_owner(TOKEN_A, plan=publishable_plan())

        def wellness_is_down(request: urllib.request.Request) -> bytes:
            if "/wellness?" in request.full_url:
                raise _http_error(request.full_url, 500)
            return FakeIntervals.__call__(self.fake, request)

        self.gateway.fetch = wellness_is_down
        status, payload = self.route("session", body={}, token=TOKEN_A)

        self.assertEqual(200, status, payload)
        self.assertEqual("passed", payload["status"])
        self.assertIsNotNone(payload["plan_state"])
        context = payload["context"]
        self.assertEqual("unknown", context["freshness"]["recovery"])
        self.assertIn("intervals_wellness_read_failed", context["unknowns"])
        self.assertEqual("passed", payload["validation"]["status"], payload["validation"])

    def test_an_activities_outage_still_ends_the_turn(self):
        """The control: the read the turn cannot be honest without still refuses."""
        self.seed_owner(TOKEN_A, plan=publishable_plan())

        def activities_are_down(request: urllib.request.Request) -> bytes:
            if "/activities?" in request.full_url:
                raise _http_error(request.full_url, 500)
            return FakeIntervals.__call__(self.fake, request)

        self.gateway.fetch = activities_are_down
        status, payload = self.route("session", body={}, token=TOKEN_A)

        self.assertEqual(502, status, payload)
        self.assertEqual("provider_error", payload["error"])

    def _wellness_answers(self, status_code: int):
        """Every read as usual, except /wellness, which answers `status_code`."""

        def fetch(request: urllib.request.Request) -> bytes:
            if "/wellness?" in request.full_url:
                raise _http_error(request.full_url, status_code)
            return FakeIntervals.__call__(self.fake, request)

        return fetch

    def test_a_wellness_permission_denial_keeps_the_turn_and_names_itself(self):
        """403 is not downtime: the athlete reconnects and it is fixed.

        Intervals grants each permission separately, so a wellness read this connection
        may not make fails identically every turn until they grant it again. The turn
        still answers -- recovery is optional evidence -- but what the coach can tell
        them has to be the repair, not "the provider is having a bad minute".
        """
        self.seed_owner(TOKEN_A, plan=publishable_plan())
        self.gateway.fetch = self._wellness_answers(403)

        status, payload = self.route("session", body={}, token=TOKEN_A)

        self.assertEqual(200, status, payload)
        self.assertIsNotNone(payload["plan_state"])
        unknowns = payload["context"]["unknowns"]
        self.assertEqual("unknown", payload["context"]["freshness"]["recovery"])
        self.assertTrue(
            any(u.startswith("intervals_wellness_permission_denied") for u in unknowns),
            unknowns,
        )
        self.assertNotIn("intervals_wellness_read_failed", unknowns)

    def test_a_wellness_401_still_reaches_the_connection_it_invalidates(self):
        """The one failure the optional read may not swallow.

        A 401 is the credential itself refused, and the gateway answers it by forgetting
        the connection -- which only happens if the error reaches it. Activities is read
        first, so this is the narrow window it could hide in: the grant revoked between
        the two reads of one turn.
        """
        self.seed_owner(TOKEN_A, plan=publishable_plan())
        self.gateway.fetch = self._wellness_answers(401)

        status, payload = self.route("session", body={}, token=TOKEN_A)

        self.assertEqual(502, status, payload)
        self.assertEqual("provider_error", payload["error"])

        # Forgotten, not merely reported: the next turn on the same token is a stranger.
        self.gateway.fetch = None
        status, _ = self.route("session", body={}, token=TOKEN_A)
        self.assertEqual(401, status)

    def test_a_plan_with_a_measured_max_hr_reads_each_endpoint_once(self):
        self.seed_owner(TOKEN_A, plan=publishable_plan())
        self.fake.sport_settings = copy.deepcopy(RUN_SPORT_SETTINGS)

        status, payload = self.route("session", body={}, token=TOKEN_A)

        self.assertEqual(200, status, payload)
        self.assertEqual("passed", payload["status"])
        self.assertEqual([], payload["reconciliation"]["applied"])
        self.assertEqual(
            ["/activities", "/wellness", "/sport-settings"], self.provider_gets()
        )

    def test_a_plan_with_no_measured_max_hr_never_asks_for_sport_settings(self):
        """Nothing to disagree with means nothing to read.

        The plan's own max HR is the only value the sport settings' figure is ever put
        beside, so with the plan carrying none the request could only be made and thrown
        away. The rest of the read is untouched.
        """
        plan = publishable_plan()
        plan["athlete_baseline"] = {**plan["athlete_baseline"], "max_hr": None}
        self.seed_owner(TOKEN_A, plan=plan)
        self.fake.sport_settings = copy.deepcopy(RUN_SPORT_SETTINGS)

        status, payload = self.route("session", body={}, token=TOKEN_A)

        self.assertEqual(200, status, payload)
        self.assertEqual("passed", payload["status"])
        self.assertEqual(["/activities", "/wellness"], self.provider_gets())

    def test_a_session_that_reconciles_still_reads_each_endpoint_once(self):
        """The rebuild reads the moved plan, not the provider, a second time."""
        plan = publishable_plan()
        quality = next(
            session
            for session in plan["week"]["sessions"]
            if session["session_id"] == "run-quality-01"
        )
        quality["execution"] = {
            "publish_supported": True,
            "external_id": "ev-quality-01",
            "delivery_state": "intervals_accepted",
        }
        self.seed_owner(TOKEN_A, plan=plan)
        self.fake.sport_settings = copy.deepcopy(RUN_SPORT_SETTINGS)
        self.fake.activities = [
            {
                "id": "i9100",
                "type": "Run",
                "start_date_local": "2026-08-13T07:00:00",
                "moving_time": 3000,
                "distance": 10000.0,
                "average_speed": 3.33,
                "average_heartrate": 160,
                "paired_event_id": "ev-quality-01",
            }
        ]

        status, payload = self.route("session", body={}, token=TOKEN_A)

        self.assertEqual(200, status, payload)
        self.assertEqual(
            ["run-quality-01"],
            [item["session_id"] for item in payload["reconciliation"]["applied"]],
        )
        self.assertEqual(2, payload["plan_state"]["plan_version"])
        # The plan moved and the context was rebuilt against it, and none of that is
        # visible from the provider's side: one of each endpoint, not two.
        self.assertEqual(
            ["/activities", "/wellness", "/activity/i9100/intervals", "/sport-settings"],
            self.provider_gets(),
        )
