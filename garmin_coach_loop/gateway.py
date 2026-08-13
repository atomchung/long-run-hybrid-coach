"""Agent-neutral HTTP transport for one athlete's own Coach Loop.

The local CLI answers "what does *this machine's* user own"; a hosted agent has to answer
"what does *this token's* athlete own", for many athletes, without either of them ever
reaching the other's state. That is the entire job of this module. It adds no coaching
path: every route ends in the same functions the CLI already calls -- ``build_context``,
``apply_reconciliation``, ``validate_bundle``, ``apply_decision``, ``prepare_delivery_set``,
``publish_delivery_set``, ``apply_delivery_observations`` -- so there is exactly one
validator, one store, and one delivery boundary in the product.

Three rules shape the code below:

- **Identity precedes everything.** Every ``/v1/coach/*`` request resolves its bearer token
  to an owner before a request body is parsed, a provider is called, or a directory is
  touched. An unknown token can therefore neither read state nor create it.
- **The store answers, the request does not.** Plan identity and version always come from
  the owner's own store; the request's ``plan_id``/``plan_version`` are only ever checked
  against it, never trusted in its place.
- **Nothing is remembered between requests.** There is no server-side proposal database:
  a proposal is bound by a content hash the client hands back, so a restarted process
  cannot forget an approval and an old approval cannot outlive the plan it described.

Secrets come from the environment only and stay there. Tokens are never logged, echoed,
stored in plaintext, or placed in a URL; upstream provider bodies are never forwarded.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import os
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable

from .context_builder import build_context
from .context_core import (
    DEFAULT_SESSION_MINUTES,
    DEFAULT_TIMEZONE,
    RED_FLAG_FIELDS,
    ContextBuildError,
    ContextRequest,
    build_window,
)
from .delivery import (
    DeliveryError,
    IntervalsTransport,
    approve_delivery_set,
    prepare_delivery_set,
    publish_delivery_set,
)
from .identity import (
    IdentityError,
    lookup_or_create_owner,
    owner_for_fingerprint,
    record_token_fingerprint,
    token_fingerprint,
)
from .reconcile import apply_reconciliation
from .source_intervals import (
    REQUEST_TIMEOUT_SECONDS,
    USER_AGENT,
    Fetcher,
    IntervalsCredentials,
)
from .store import (
    StateStoreError,
    apply_decision,
    apply_delivery_observations,
    canonical_hash,
    plan_diff,
    read_current_plan,
    resolve_state_dir,
    resolve_state_root,
)
from .validation import validate_bundle


LOGGER = logging.getLogger(__name__)

API_VERSION = "1.0"
PROVIDER = "intervals"
INTERVALS_TOKEN_URL = "https://intervals.icu/api/oauth/token"

# With OAuth credentials the athlete id in a path is always "0": Intervals resolves it to
# whichever athlete the bearer token belongs to. Carrying a real athlete id here would let
# one token address another athlete's routes.
OAUTH_ATHLETE_ID = "0"

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8422
MAX_REQUEST_BYTES = 1 * 1024 * 1024
# A refused body still has to be consumed, or the client gets a connection reset instead
# of the reason it was refused. The budget is bounded so consuming it is never the attack.
MAX_DRAIN_BYTES = 2 * MAX_REQUEST_BYTES

# The transport terminates at the furthest hop this product can observe. Intervals ->
# Garmin Connect -> device are external hops it cannot read back, so no response ever
# names them (AGENTS.md invariant 8).
MAX_DELIVERY_STATE = "intervals_accepted"

STATE_ROOT_ENV_VAR = "GARMIN_COACH_LOOP_GATEWAY_STATE_ROOT"
TOKEN_HMAC_KEY_ENV_VAR = "GARMIN_COACH_LOOP_TOKEN_HMAC_KEY"
CLIENT_ID_ENV_VAR = "GARMIN_COACH_LOOP_INTERVALS_CLIENT_ID"
CLIENT_SECRET_ENV_VAR = "GARMIN_COACH_LOOP_INTERVALS_CLIENT_SECRET"
REQUIRED_ENV_VARS = (
    STATE_ROOT_ENV_VAR,
    TOKEN_HMAC_KEY_ENV_VAR,
    CLIENT_ID_ENV_VAR,
    CLIENT_SECRET_ENV_VAR,
)
MIN_HMAC_KEY_CHARACTERS = 32

FATIGUE_LEVELS = ("normal", "elevated", "severe", "unknown")


class GatewayConfigError(RuntimeError):
    """The gateway cannot start with the configuration it was given."""


class GatewayError(RuntimeError):
    """One request is refused with an exact status and a machine-readable code.

    ``detail`` is only ever this product's own text (a validation message, a store or
    delivery block). Provider bodies, request payloads and credentials never reach it.
    """

    def __init__(
        self,
        status: HTTPStatus | int,
        code: str,
        detail: str | None = None,
        *,
        oauth: bool = False,
        extra: dict[str, Any] | None = None,
    ):
        super().__init__(f"{int(status)} {code}")
        self.status = int(status)
        self.code = code
        self.detail = detail
        self.oauth = oauth
        self.extra = extra or {}

    def payload(self) -> dict[str, Any]:
        if self.oauth:
            # RFC 6749 shape, and nothing else: an OAuth client reads `error` and a
            # richer body here would only describe the athlete's provider account.
            return {"error": self.code}
        body: dict[str, Any] = {"status": "blocked", "error": self.code}
        if self.detail:
            body["detail"] = self.detail
        body.update(self.extra)
        return body


@dataclass(frozen=True)
class GatewayConfig:
    """Everything the process needs, read once at startup and never re-read per request."""

    state_root: Path
    token_hmac_key: bytes
    intervals_client_id: str
    intervals_client_secret: str
    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT

    @property
    def identity_db_path(self) -> Path:
        return self.state_root / "identity.db"


def load_config(
    env: dict[str, str] | None = None,
    *,
    host: str | None = None,
    port: int | None = None,
) -> GatewayConfig:
    """Build the startup configuration, refusing to run rather than guessing.

    There is deliberately no fallback to ``GARMIN_COACH_LOOP_HOME``: that variable names
    one person's own store, and a server that quietly adopted it would serve every athlete
    out of that one directory. Errors name the missing variable and never its value.
    """
    source = os.environ if env is None else env
    missing = [name for name in REQUIRED_ENV_VARS if not str(source.get(name) or "").strip()]
    if missing:
        raise GatewayConfigError(
            "gateway configuration is incomplete; set " + ", ".join(missing)
        )
    key = str(source[TOKEN_HMAC_KEY_ENV_VAR]).strip()
    if len(key) < MIN_HMAC_KEY_CHARACTERS:
        raise GatewayConfigError(
            f"{TOKEN_HMAC_KEY_ENV_VAR} must be at least {MIN_HMAC_KEY_CHARACTERS} characters"
        )
    try:
        state_root = resolve_state_root(str(source[STATE_ROOT_ENV_VAR]).strip())
    except StateStoreError as exc:
        raise GatewayConfigError(f"{STATE_ROOT_ENV_VAR} is unusable: {exc}") from exc
    return GatewayConfig(
        state_root=state_root,
        token_hmac_key=key.encode("utf-8"),
        intervals_client_id=str(source[CLIENT_ID_ENV_VAR]).strip(),
        intervals_client_secret=str(source[CLIENT_SECRET_ENV_VAR]).strip(),
        host=host or DEFAULT_HOST,
        port=DEFAULT_PORT if port is None else int(port),
    )


# --------------------------------------------------------------------------------------
# Request-shape helpers: every one of them fails closed rather than coercing
# --------------------------------------------------------------------------------------


def _invalid(detail: str) -> GatewayError:
    return GatewayError(HTTPStatus.BAD_REQUEST, "invalid_request", detail)


def _object_field(body: dict[str, Any], field: str) -> dict[str, Any]:
    value = body.get(field)
    if not isinstance(value, dict):
        raise _invalid(f"{field} must be a JSON object")
    return value


def _string_field(body: dict[str, Any], field: str) -> str:
    value = body.get(field)
    if not isinstance(value, str) or not value.strip():
        raise _invalid(f"{field} must be a non-empty string")
    return value


def _integer_field(body: dict[str, Any], field: str) -> int:
    value = body.get(field)
    if isinstance(value, bool) or not isinstance(value, int):
        raise _invalid(f"{field} must be an integer")
    return value


def _string_list_field(body: dict[str, Any], field: str) -> list[str]:
    value = body.get(field)
    if not isinstance(value, list) or not value:
        raise _invalid(f"{field} must be a non-empty array of strings")
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise _invalid(f"{field} must contain only non-empty strings")
    return list(value)


def _optional_bool(body: dict[str, Any], field: str) -> bool | None:
    value = body.get(field)
    if value is None or isinstance(value, bool):
        return value
    raise _invalid(f"{field} must be true, false or null")


def _context_request(body: dict[str, Any]) -> ContextRequest:
    """Map the athlete-input half of a session request onto the existing ContextRequest.

    Omission stays unknown throughout: no available day list means availability is not
    confirmed, no red flag value means unassessed. Neither becomes a convenient default.
    """
    red_flags: dict[str, bool | None] = {
        field: (False if body.get("all_clear") is True else None) for field in RED_FLAG_FIELDS
    }
    raw_flags = body.get("red_flags")
    if raw_flags is not None:
        if not isinstance(raw_flags, dict):
            raise _invalid("red_flags must be an object")
        for key, value in raw_flags.items():
            if key not in RED_FLAG_FIELDS:
                raise _invalid(f"unknown red flag: {key!r}")
            if value is not None and not isinstance(value, bool):
                raise _invalid(f"red_flags.{key} must be true, false or null")
            red_flags[key] = value

    raw_days = body.get("available_days")
    if raw_days is None:
        available_days: list[str] = []
    elif isinstance(raw_days, list) and all(isinstance(day, str) for day in raw_days):
        available_days = [day.strip().lower() for day in raw_days if day.strip()]
    else:
        raise _invalid("available_days must be an array of weekday names")

    session_minutes = body.get("session_minutes", DEFAULT_SESSION_MINUTES)
    if session_minutes is not None and (
        isinstance(session_minutes, bool) or not isinstance(session_minutes, int)
    ):
        raise _invalid("session_minutes must be an integer or null")

    levels: dict[str, str] = {}
    for field in ("leg_fatigue", "soreness"):
        value = body.get(field, "unknown")
        if value not in FATIGUE_LEVELS:
            raise _invalid(f"{field} must be one of {list(FATIGUE_LEVELS)}")
        levels[field] = value

    raw_unknowns = body.get("unknowns") or []
    if not isinstance(raw_unknowns, list) or not all(
        isinstance(item, str) for item in raw_unknowns
    ):
        raise _invalid("unknowns must be an array of strings")

    as_of = body.get("as_of")
    if as_of is not None and not isinstance(as_of, str):
        raise _invalid("as_of must be an ISO-8601 string or null")
    timezone_name = body.get("timezone", DEFAULT_TIMEZONE)
    if not isinstance(timezone_name, str) or not timezone_name.strip():
        raise _invalid("timezone must be a non-empty string")

    return ContextRequest(
        as_of_raw=as_of,
        timezone_name=timezone_name,
        available_days=available_days,
        session_minutes=session_minutes,
        red_flags=red_flags,
        leg_fatigue=levels["leg_fatigue"],
        soreness=levels["soreness"],
        schedule_changed=_optional_bool(body, "schedule_changed"),
        equipment_changed=_optional_bool(body, "equipment_changed"),
        extra_unknowns=list(raw_unknowns),
    )


def _bearer_token(raw: str | None) -> str | None:
    if not isinstance(raw, str):
        return None
    scheme, _, value = raw.partition(" ")
    if scheme.strip().lower() != "bearer":
        return None
    token = value.strip()
    return token or None


def _utc_iso(moment: dt.datetime) -> str:
    return (
        moment.astimezone(dt.timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _validation_summary(report: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(report, dict):
        return None
    return {
        "status": report.get("status"),
        "errors": list(report.get("errors") or []),
        "warnings": list(report.get("warnings") or []),
    }


def _delivery_view(plan: dict[str, Any]) -> dict[str, Any]:
    """Observable delivery state per session -- what the product can actually see."""
    sessions = [
        {
            "session_id": session.get("session_id"),
            "scheduled_date": session.get("scheduled_date"),
            "sport": session.get("sport"),
            "match_status": session.get("match_status"),
            "publish_supported": (session.get("execution") or {}).get("publish_supported"),
            "delivery_state": (session.get("execution") or {}).get("delivery_state"),
            "external_id": (session.get("execution") or {}).get("external_id"),
        }
        for session in (plan.get("week") or {}).get("sessions", [])
        if isinstance(session, dict)
    ]
    return {"max_delivery_state": MAX_DELIVERY_STATE, "sessions": sessions}


def _proposal_hash(
    *, plan_id: str, base_version: int, after_plan: dict[str, Any], decision_event: dict[str, Any]
) -> str:
    """Bind a proposal to its exact material without storing it anywhere.

    The base version is inside the hash, so a proposal prepared against v3 can never be
    replayed onto v4 even if its plan and event bytes are unchanged.
    """
    return canonical_hash(
        {
            "plan_id": plan_id,
            "base_version": base_version,
            "after_plan": after_plan,
            "decision_event": decision_event,
        }
    )


# --------------------------------------------------------------------------------------
# The service: routes, provider calls, and the owner boundary
# --------------------------------------------------------------------------------------


class CoachGateway:
    """Route handling with no coaching logic of its own.

    ``fetch`` is the single injection seam, following the convention in
    ``source_intervals``: one callable taking a prepared ``urllib.request.Request`` and
    returning the raw body. It covers the OAuth token exchange, the provider reads behind
    ``build_context``, and the delivery transport alike, so a test can assert that a
    refused request never reached the provider at all.
    """

    def __init__(
        self,
        config: GatewayConfig,
        *,
        fetch: Fetcher | None = None,
        now: Callable[[], dt.datetime] | None = None,
    ):
        self.config = config
        self.fetch = fetch
        self._now = now or (lambda: dt.datetime.now(dt.timezone.utc))

    # -- infrastructure ---------------------------------------------------------------

    def health(self) -> dict[str, Any]:
        """Liveness only. No provider call, no state read, no owner -- so an uptime check
        can never be the thing that creates or touches somebody's store."""
        return {"status": "ok", "api_version": API_VERSION}

    def resolve_owner(self, token: str | None) -> str:
        if token is None:
            raise GatewayError(HTTPStatus.UNAUTHORIZED, "unauthorized")
        fingerprint = token_fingerprint(token, hmac_key=self.config.token_hmac_key)
        owner_id = owner_for_fingerprint(self.config.identity_db_path, fingerprint)
        if owner_id is None:
            raise GatewayError(HTTPStatus.UNAUTHORIZED, "unauthorized")
        return owner_id

    def route(self, kind: str, owner_id: str, token: str, body: dict[str, Any]) -> dict[str, Any]:
        handlers: dict[str, Callable[[str, str, dict[str, Any]], dict[str, Any]]] = {
            "session": self.start_session,
            "decision_prepare": self.prepare_decision,
            "decision_apply": self.apply_decision_request,
            "delivery_prepare": self.prepare_delivery,
            "delivery_publish": self.publish_delivery,
        }
        try:
            return handlers[kind](owner_id, token, body)
        except GatewayError:
            raise
        except StateStoreError as exc:
            raise GatewayError(HTTPStatus.CONFLICT, "state_conflict", str(exc)) from exc
        except DeliveryError as exc:
            raise GatewayError(HTTPStatus.CONFLICT, "delivery_blocked", str(exc)) from exc
        except ContextBuildError as exc:
            # A revoked or rejected token lands here. It is reported as an explicit
            # failure and nothing in the store is read differently, cleared, or defaulted
            # (AGENTS.md invariant 3).
            raise GatewayError(HTTPStatus.BAD_GATEWAY, "provider_error", str(exc)) from exc
        except IdentityError as exc:
            raise GatewayError(HTTPStatus.INTERNAL_SERVER_ERROR, "internal_error") from exc

    def _state_dir(self, owner_id: str) -> Path:
        return resolve_state_dir(owner_id, state_root=self.config.state_root)

    def _credentials(self, token: str) -> IntervalsCredentials:
        return IntervalsCredentials(token, OAUTH_ATHLETE_ID, "bearer")

    def _envelope(self) -> dict[str, Any]:
        return {"api_version": API_VERSION, "generated_at": _utc_iso(self._now())}

    # -- OAuth token proxy ------------------------------------------------------------

    def exchange_token(self, form: dict[str, str]) -> dict[str, Any]:
        """Exchange one authorization code, then remember only who the token belongs to.

        The agent platform runs the authorize step against Intervals directly and calls
        this as its token URL. The client secret stays here, on the server, and the
        response carries the standard OAuth fields only -- echoing the athlete block back
        would hand the agent provider account details it has no use for.
        """
        grant_type = form.get("grant_type")
        if grant_type == "refresh_token":
            # Intervals issues no refresh tokens; a client whose token stopped working
            # re-authorizes from scratch. Saying so plainly makes the client re-authorize
            # instead of retrying forever.
            raise GatewayError(HTTPStatus.BAD_REQUEST, "invalid_grant", oauth=True)
        if grant_type != "authorization_code":
            raise GatewayError(HTTPStatus.BAD_REQUEST, "unsupported_grant_type", oauth=True)
        code = form.get("code")
        if not isinstance(code, str) or not code.strip():
            raise GatewayError(HTTPStatus.BAD_REQUEST, "invalid_request", oauth=True)

        payload = self._post_form(
            INTERVALS_TOKEN_URL,
            {
                "client_id": self.config.intervals_client_id,
                "client_secret": self.config.intervals_client_secret,
                "code": code,
            },
        )
        access_token = payload.get("access_token")
        athlete = payload.get("athlete") if isinstance(payload.get("athlete"), dict) else {}
        athlete_id = athlete.get("id")
        if not isinstance(access_token, str) or not access_token:
            raise GatewayError(HTTPStatus.BAD_GATEWAY, "server_error", oauth=True)
        if athlete_id is None or not str(athlete_id).strip():
            # Without a stable athlete identity there is no way to say which store this
            # token may open, and guessing would be the one mistake that mixes athletes.
            raise GatewayError(HTTPStatus.BAD_GATEWAY, "server_error", oauth=True)

        try:
            owner_id = lookup_or_create_owner(
                self.config.identity_db_path, PROVIDER, str(athlete_id).strip()
            )
            record_token_fingerprint(
                self.config.identity_db_path,
                token_fingerprint(access_token, hmac_key=self.config.token_hmac_key),
                owner_id,
                PROVIDER,
            )
        except IdentityError as exc:
            raise GatewayError(HTTPStatus.BAD_GATEWAY, "server_error", oauth=True) from exc

        scope = payload.get("scope")
        return {
            "token_type": "Bearer",
            "access_token": access_token,
            "scope": scope if isinstance(scope, str) else "",
        }

    def _post_form(self, url: str, form: dict[str, str]) -> dict[str, Any]:
        data = urllib.parse.urlencode(form).encode("utf-8")
        request = urllib.request.Request(url, data=data, method="POST")
        request.add_header("Content-Type", "application/x-www-form-urlencoded")
        request.add_header("Accept", "application/json")
        request.add_header("User-Agent", USER_AGENT)
        try:
            if self.fetch is None:
                with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
                    body = response.read()
            else:
                body = self.fetch(request)
            value = json.loads(body)
        except (urllib.error.URLError, json.JSONDecodeError, TypeError, ValueError) as exc:
            # Deliberately opaque, and deliberately unlogged: an upstream token-endpoint
            # body can contain the code, the secret, or an athlete's account details.
            raise GatewayError(HTTPStatus.BAD_GATEWAY, "server_error", oauth=True) from exc
        if not isinstance(value, dict):
            raise GatewayError(HTTPStatus.BAD_GATEWAY, "server_error", oauth=True)
        return value

    # -- coach routes -----------------------------------------------------------------

    def start_session(self, owner_id: str, token: str, body: dict[str, Any]) -> dict[str, Any]:
        """Read the latest evidence and return the current state, exactly as the CLI does.

        This is ``refresh-context``: fetch the provider domain with this request's own
        credentials, build the context, apply the existing deterministic identity-backed
        reconciliation, and rebuild once if reconciliation moved the plan. No coaching
        fact, score or recommendation is added here.
        """
        state_dir = self._state_dir(owner_id)
        if not (state_dir / "store.json").is_file():
            # An empty account is a fact to report, not a store to create. Initialising a
            # plan is a coaching decision, and this transport never makes one.
            return {
                "status": "no_plan_state",
                **self._envelope(),
                "context_id": None,
                "plan_state": {
                    "present": False,
                    "plan_id": None,
                    "plan_version": None,
                    "current_plan": None,
                },
                "context": None,
                "validation": None,
                "unknowns": ["no PlanState exists for this account"],
                "delivery": None,
                "reconciliation": None,
            }

        request = _context_request(body)
        try:
            build_window(request, self._now())
        except ContextBuildError as exc:
            # A bad timezone or as_of is a malformed request, not a provider outage.
            raise _invalid(str(exc)) from exc

        report = self._build_context(request, state_dir, token)
        reconciliation = apply_reconciliation(state_dir, report["context"])
        if reconciliation["status"] != "passed":
            raise GatewayError(
                HTTPStatus.UNPROCESSABLE_ENTITY,
                "reconciliation_blocked",
                extra={"reconciliation": reconciliation},
            )
        if reconciliation["applied"]:
            report = self._build_context(request, state_dir, token)

        context = report["context"]
        current = read_current_plan(state_dir)
        plan = current["current_plan"]
        return {
            "status": "passed",
            **self._envelope(),
            "context_id": context.get("context_id"),
            "plan_state": {
                "present": True,
                "plan_id": current["plan_id"],
                "plan_version": current["current_version"],
                "current_plan": plan,
            },
            "context": context,
            "validation": _validation_summary(report.get("validation")),
            "unknowns": list(context.get("unknowns") or []),
            "delivery": _delivery_view(plan),
            "reconciliation": reconciliation,
        }

    def _build_context(
        self, request: ContextRequest, state_dir: Path, token: str
    ) -> dict[str, Any]:
        report = build_context(
            request,
            state_dir=state_dir,
            source="intervals",
            credentials=self._credentials(token),
            fetch=self.fetch,
            # The optional local evidence groups belong to one machine's owner, not to
            # whoever this request is serving.
            use_local_health_db=False,
            now=self._now(),
        )
        if report.get("status") != "passed":
            raise GatewayError(
                HTTPStatus.UNPROCESSABLE_ENTITY,
                "context_blocked",
                extra={"validation": _validation_summary(report.get("validation"))},
            )
        return report

    def prepare_decision(self, owner_id: str, token: str, body: dict[str, Any]) -> dict[str, Any]:
        """Preview one candidate PlanState against the store. Writes nothing, ever."""
        state_dir = self._state_dir(owner_id)
        context = _object_field(body, "context")
        after = _object_field(body, "after_plan")
        event = _object_field(body, "decision_event")
        plan_id = _string_field(body, "plan_id")
        plan_version = _integer_field(body, "plan_version")

        current = read_current_plan(state_dir)
        before = current["current_plan"]
        self._require_current(current, plan_id, plan_version)

        validation = validate_bundle(context, before, after, event)
        if validation["status"] != "passed":
            raise GatewayError(
                HTTPStatus.UNPROCESSABLE_ENTITY,
                "validation_failed",
                extra={"validation": _validation_summary(validation)},
            )
        unknowns = sorted(
            set(context.get("unknowns") or []) | set(event.get("unknowns") or [])
        )
        return {
            "status": "passed",
            **self._envelope(),
            "plan_id": current["plan_id"],
            "base_version": current["current_version"],
            "resulting_version": after.get("version"),
            "proposal_hash": _proposal_hash(
                plan_id=plan_id,
                base_version=plan_version,
                after_plan=after,
                decision_event=event,
            ),
            "diff": plan_diff(before, after),
            "validation": _validation_summary(validation),
            "warnings": list(validation.get("warnings") or []),
            "unknowns": unknowns,
        }

    def apply_decision_request(
        self, owner_id: str, token: str, body: dict[str, Any]
    ) -> dict[str, Any]:
        """Commit the exact bundle that was previewed, or refuse to commit anything."""
        state_dir = self._state_dir(owner_id)
        context = _object_field(body, "context")
        after = _object_field(body, "after_plan")
        event = _object_field(body, "decision_event")
        plan_id = _string_field(body, "plan_id")
        plan_version = _integer_field(body, "plan_version")
        proposal_hash = _string_field(body, "proposal_hash")

        expected_hash = _proposal_hash(
            plan_id=plan_id, base_version=plan_version, after_plan=after, decision_event=event
        )
        if expected_hash != proposal_hash:
            raise GatewayError(HTTPStatus.CONFLICT, "proposal_hash_mismatch")

        current = read_current_plan(state_dir)
        receipt = current["receipt"]
        if (
            receipt.get("event_id") == event.get("event_id")
            and receipt.get("event_hash") == canonical_hash(event)
            and receipt.get("plan_hash") == canonical_hash(after)
        ):
            # This exact decision is already the head. A retried request -- a dropped
            # response, a client redial -- must read as the success it actually was, not
            # as a stale-version conflict.
            return {
                "status": "passed",
                **self._envelope(),
                "plan_id": current["plan_id"],
                "plan_version": current["current_version"],
                "event_id": receipt.get("event_id"),
                "idempotent_replay": True,
                "proposal_hash": proposal_hash,
                "validation": {"status": "passed", "errors": [], "warnings": []},
            }

        self._require_current(current, plan_id, plan_version)
        validation = validate_bundle(context, current["current_plan"], after, event)
        if validation["status"] != "passed":
            raise GatewayError(
                HTTPStatus.UNPROCESSABLE_ENTITY,
                "validation_failed",
                extra={"validation": _validation_summary(validation)},
            )
        result = apply_decision(state_dir, context=context, after=after, event=event)
        return {
            "status": "passed",
            **self._envelope(),
            "plan_id": result["plan_id"],
            "plan_version": result["current_version"],
            "event_id": event.get("event_id"),
            "idempotent_replay": result["idempotent_replay"],
            "proposal_hash": proposal_hash,
            "validation": _validation_summary(result.get("validation")),
        }

    def prepare_delivery(self, owner_id: str, token: str, body: dict[str, Any]) -> dict[str, Any]:
        """Derive the exact preview set for sessions of the *current* plan. No writes."""
        state_dir = self._state_dir(owner_id)
        plan_id = _string_field(body, "plan_id")
        plan_version = _integer_field(body, "plan_version")
        session_ids = _string_list_field(body, "session_ids")

        current = read_current_plan(state_dir)
        self._require_current(current, plan_id, plan_version)
        proposal_set = prepare_delivery_set(current["current_plan"], session_ids)
        return {
            "status": "passed",
            **self._envelope(),
            "plan_id": proposal_set["plan_id"],
            "plan_version": proposal_set["plan_version"],
            "proposal_id": proposal_set["proposal_id"],
            "proposal_hash": proposal_set["proposal_hash"],
            "confirmation_required": True,
            "preview": [
                {
                    "session_id": item["session_id"],
                    "scheduled_date": item["workout"]["scheduled_date"],
                    "sport": item["workout"]["sport"],
                    "name": item["workout"]["name"],
                    "plan_prescription": item["preview"]["plan_prescription"],
                    "delivered_description": item["preview"]["delivered_description"],
                    "owned_external_id": item["owned_external_id"],
                    "proposal_hash": item["proposal_hash"],
                }
                for item in proposal_set["items"]
            ],
            "delivery_set": proposal_set,
        }

    def publish_delivery(self, owner_id: str, token: str, body: dict[str, Any]) -> dict[str, Any]:
        """Publish one confirmed set through the existing binding, dedupe and read-back."""
        state_dir = self._state_dir(owner_id)
        delivery_set = _object_field(body, "delivery_set")
        proposal_hash = _string_field(body, "proposal_hash")
        if body.get("confirmed") is not True:
            raise GatewayError(HTTPStatus.CONFLICT, "confirmation_required")
        if delivery_set.get("proposal_hash") != proposal_hash:
            # Approval is bound to the exact proposal, so a set whose content no longer
            # hashes to what was confirmed is not an approved delivery at all.
            raise GatewayError(HTTPStatus.CONFLICT, "proposal_hash_mismatch")

        approval = approve_delivery_set(delivery_set, approved_by=f"owner:{owner_id}")
        transport = IntervalsTransport(self._credentials(token), fetch=self.fetch)
        receipt = publish_delivery_set(
            delivery_set,
            approval,
            load_current_plan=lambda: read_current_plan(state_dir)["current_plan"],
            transport=transport,
        )
        state_update = apply_delivery_observations(
            state_dir, observations=receipt["observations"]
        )
        return {
            "status": "passed",
            **self._envelope(),
            "delivery_state": receipt["delivery_state"],
            "max_delivery_state": MAX_DELIVERY_STATE,
            "plan_id": state_update["plan_id"],
            "plan_version": state_update["current_version"],
            "receipt_id": receipt["receipt_id"],
            "proposal_hash": receipt["proposal_hash"],
            "delivered": [
                {
                    "session_id": item["observation"]["session_id"],
                    "external_id": item["observation"]["external_id"],
                    "owned_external_id": item["owned_external_id"],
                    "operation": item["operation"],
                    "delivery_state": item["delivery_state"],
                }
                for item in receipt["item_receipts"]
            ],
            "state_update": {
                "event_id": state_update.get("event_id"),
                "idempotent_replay": state_update["idempotent_replay"],
                "session_ids": state_update["session_ids"],
                "external_ids": state_update["external_ids"],
            },
        }

    @staticmethod
    def _require_current(current: dict[str, Any], plan_id: str, plan_version: int) -> None:
        """The store decides what is current; the request only says what it expected."""
        if current["plan_id"] != plan_id:
            raise GatewayError(
                HTTPStatus.CONFLICT,
                "plan_mismatch",
                extra={"current_plan_id": current["plan_id"]},
            )
        if current["current_version"] != plan_version:
            raise GatewayError(
                HTTPStatus.CONFLICT,
                "stale_plan_version",
                extra={"current_plan_version": current["current_version"]},
            )


# --------------------------------------------------------------------------------------
# HTTP layer
# --------------------------------------------------------------------------------------


# path -> (allowed method, route kind). Unknown paths 404, known paths with the wrong
# method 405; neither reaches an owner or a provider.
ROUTES: dict[str, tuple[str, str]] = {
    "/healthz": ("GET", "health"),
    "/oauth/intervals/token": ("POST", "token"),
    "/v1/coach/session": ("POST", "session"),
    "/v1/coach/decision/prepare": ("POST", "decision_prepare"),
    "/v1/coach/decision/apply": ("POST", "decision_apply"),
    "/v1/coach/delivery/prepare": ("POST", "delivery_prepare"),
    "/v1/coach/delivery/publish": ("POST", "delivery_publish"),
}


class CoachGatewayHandler(BaseHTTPRequestHandler):
    """One request at a time, with the body read only after the caller is known."""

    server_version = "garmin-coach-loop-gateway"
    sys_version = ""

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler naming
        self._dispatch("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._dispatch("POST")

    def do_PUT(self) -> None:  # noqa: N802
        self._dispatch("PUT")

    def do_PATCH(self) -> None:  # noqa: N802
        self._dispatch("PATCH")

    def do_DELETE(self) -> None:  # noqa: N802
        self._dispatch("DELETE")

    def do_HEAD(self) -> None:  # noqa: N802
        self._dispatch("HEAD")

    def do_OPTIONS(self) -> None:  # noqa: N802
        self._dispatch("OPTIONS")

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - base signature
        """Drop the default access log.

        It prints the raw request line, which is one query string away from writing a
        credential into a log file. ``_dispatch`` logs method, path, status and owner
        instead -- and this design never puts a token in a URL, which is what keeps that
        line safe to write.
        """

    def _dispatch(self, method: str) -> None:
        owner_id: str | None = None
        path = urllib.parse.urlsplit(self.path).path
        try:
            route = ROUTES.get(path)
            if route is None:
                raise GatewayError(HTTPStatus.NOT_FOUND, "not_found")
            allowed_method, kind = route
            if method != allowed_method:
                raise GatewayError(HTTPStatus.METHOD_NOT_ALLOWED, "method_not_allowed")
            gateway: CoachGateway = self.server.gateway  # type: ignore[attr-defined]
            if kind == "health":
                payload = gateway.health()
            elif kind == "token":
                payload = gateway.exchange_token(self._form_body())
            else:
                # Identity first: before the body is parsed, before the provider is
                # called, and before any path under the state root is touched.
                token = _bearer_token(self.headers.get("Authorization"))
                owner_id = gateway.resolve_owner(token)
                payload = gateway.route(kind, owner_id, str(token), self._json_body())
            status = HTTPStatus.OK
        except GatewayError as exc:
            status, payload = exc.status, exc.payload()
        except Exception:  # pragma: no cover - defensive; never leaks the cause
            LOGGER.exception("unhandled gateway failure on %s %s", method, path)
            status = HTTPStatus.INTERNAL_SERVER_ERROR
            payload = {"status": "blocked", "error": "internal_error"}
        self._send_json(int(status), payload)
        LOGGER.info("%s %s -> %s owner=%s", method, path, int(status), owner_id or "-")

    def _read_body(self, expected_type: str) -> bytes:
        raw_length = self.headers.get("Content-Length")
        if raw_length is None:
            return b""
        try:
            length = int(raw_length)
        except ValueError as exc:
            raise _invalid("Content-Length must be an integer") from exc
        if length < 0:
            raise _invalid("Content-Length must not be negative")
        if length > MAX_REQUEST_BYTES:
            raise GatewayError(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "payload_too_large")
        if length == 0:
            return b""
        media_type = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if media_type != expected_type:
            raise GatewayError(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, "unsupported_media_type")
        data = self.rfile.read(length)
        self._body_read = True
        if len(data) != length:
            raise _invalid("request body was truncated")
        return data

    def _json_body(self) -> dict[str, Any]:
        raw = self._read_body("application/json")
        if not raw:
            return {}
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise _invalid("request body must be UTF-8 JSON") from exc
        if not isinstance(value, dict):
            raise _invalid("request body must be a JSON object")
        return value

    def _form_body(self) -> dict[str, str]:
        raw = self._read_body("application/x-www-form-urlencoded")
        try:
            parsed = urllib.parse.parse_qs(raw.decode("utf-8"), keep_blank_values=True)
        except UnicodeDecodeError as exc:
            raise GatewayError(HTTPStatus.BAD_REQUEST, "invalid_request", oauth=True) from exc
        return {key: values[0] for key, values in parsed.items() if values}

    def _drain(self) -> None:
        """Consume an unread body so the client can read the response instead of a reset."""
        if getattr(self, "_body_read", False):
            return
        self._body_read = True
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return
        remaining = min(max(length, 0), MAX_DRAIN_BYTES)
        while remaining > 0:
            chunk = self.rfile.read(min(remaining, 65536))
            if not chunk:
                return
            remaining -= len(chunk)

    def _send_json(self, status: int, payload: dict[str, Any]) -> None:
        self._drain()
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)


class CoachGatewayServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: tuple[str, int], handler_class: type, *, gateway: CoachGateway):
        self.gateway = gateway
        super().__init__(address, handler_class)


def run_gateway(config: GatewayConfig, *, gateway: CoachGateway | None = None) -> None:
    """Serve until interrupted. Binds loopback: TLS belongs to the tunnel in front."""
    config.state_root.mkdir(parents=True, mode=0o700, exist_ok=True)
    server = CoachGatewayServer(
        (config.host, config.port),
        CoachGatewayHandler,
        gateway=gateway or CoachGateway(config),
    )
    LOGGER.info("gateway listening on %s:%s", config.host, server.server_address[1])
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
