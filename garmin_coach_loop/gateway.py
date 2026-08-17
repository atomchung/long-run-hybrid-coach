"""Agent-neutral HTTP transport for one athlete's own Coach Loop.

The local CLI answers "what does *this machine's* user own"; a hosted agent has to answer
"what does *this token's* athlete own", for many athletes, without either of them ever
reaching the other's state. That is the entire job of this module. It adds no coaching
path: every route ends in the same functions the CLI already calls -- ``build_context``,
``apply_reconciliation``, ``validate_bundle``, ``apply_decision``, ``prepare_delivery_set``,
``deliver_approved_set`` -- so there is exactly one validator, one store, and one delivery
boundary in the product.

Three rules shape the code below:

- **Identity precedes everything.** Every ``/v1/coach/*`` request resolves its bearer token
  to an owner before a request body is parsed, a provider is called, or a directory is
  touched. An unknown token can therefore neither read state nor create it.
- **The store answers, the request does not.** Plan identity and version always come from
  the owner's own store; the request's ``plan_id``/``plan_version`` are only ever checked
  against it, never trusted in its place.
- **Nothing is remembered between requests.** There is no server-side proposal database: a
  proposal is a signed, expiring statement of what was previewed, which the client hands
  back. A restarted process therefore cannot forget an approval, and an approval cannot
  outlive the owner, evidence, or plan version it was issued against.

Secrets come from the environment only and stay there. A provider token is never logged
and never written down, in any form: the identity registry keeps a keyed fingerprint, and
the MCP entry keeps the token itself inside a sealed envelope it hands to the client
rather than in a table (see ``token_envelope``). The one place a credential reaches a URL
is that envelope, as the authorization code of a standard OAuth redirect, encrypted and
good for a minute. Upstream provider bodies are never forwarded.
"""

from __future__ import annotations

import base64
import datetime as dt
import hashlib
import hmac
import json
import logging
import os
import re
import signal
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from . import athlete_evidence, mcp_transport, security_log, token_envelope
from .athlete_evidence import AthleteEvidenceError
from .context_builder import build_context
from .context_core import (
    DEFAULT_SESSION_MINUTES,
    DEFAULT_TIMEZONE,
    RED_FLAG_FIELDS,
    BuildWindow,
    ContextBuildError,
    ContextRequest,
    build_window,
    coverage_entry,
)
from .delivery import (
    DeliveryError,
    IntervalsTransport,
    approve_delivery_set,
    approve_withdrawal_set,
    deliver_approved_set,
    prepare_delivery_set,
    prepare_withdrawal_set,
    withdraw_approved_set,
)
from .identity import (
    IdentityError,
    ensure_registry,
    lookup_or_create_owner,
    owner_for_fingerprint,
    record_token_fingerprint,
    revoked_after,
    scopes_for_fingerprint,
    token_fingerprint,
)
from .plan_change import ChangeRequestError, project_change_request
from .plan_init import project_initialization_request
from .proposals import ProposalError, binding, issue_proposal, open_proposal
from .reconcile import apply_reconciliation
from .release_identity import (
    DEPLOYMENT_ENVIRONMENT_ENV_VAR,
    DEPLOYMENT_INSTANCE_ID_ENV_VAR,
    ReleaseIdentityError,
    make_deployment_identity,
    package_artifact_sha256,
    release_identity,
)
from .source_intervals import (
    BASE_URL,
    REQUEST_TIMEOUT_SECONDS,
    USER_AGENT,
    Fetcher,
    IntervalsCredentials,
    authorization_header,
    fetch_domain,
)
from .store import (
    StateStoreError,
    apply_decision,
    canonical_hash,
    close_delivery_attempt,
    init_store,
    pending_delivery_attempt,
    read_current_plan,
    resolve_state_dir,
    resolve_state_root,
    unresolved_delivery_operations,
)
from .token_envelope import EnvelopeError
from .validation import validate_adopted_plan, validate_bundle, validate_plan_state


LOGGER = logging.getLogger(__name__)

API_VERSION = "1.0"
PROVIDER = "intervals"
INTERVALS_TOKEN_URL = "https://intervals.icu/api/oauth/token"
INTERVALS_AUTHORIZE_URL = "https://intervals.icu/oauth/authorize"
SPORT_SETTINGS_PATH = "/sport-settings"
_SCOPE_NAME = re.compile(r"^[A-Z][A-Z0-9_]*:[A-Z][A-Z0-9_]*$")

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
# Optional, unlike the four above: a bind address and port always have a value -- the
# loopback default -- so there is nothing to refuse startup over when neither the flag nor
# the variable is set. A hosting platform that assigns its own port sets the variable;
# `--host`/`--port` still win when the operator passes them explicitly (see `load_config`).
HOST_ENV_VAR = "GARMIN_COACH_LOOP_GATEWAY_HOST"
PORT_ENV_VAR = "GARMIN_COACH_LOOP_GATEWAY_PORT"
# Optional for the same reason: `/mcp` already answers to its own origin and to the
# connector host below, so a deployment that adds no browser origin sets nothing.
MCP_ORIGINS_ENV_VAR = "GARMIN_COACH_LOOP_MCP_ALLOWED_ORIGINS"
# Optional, and the way a new hosted agent is admitted: the callback origins this
# deployment will register a remote MCP client for, on top of the validated ones below.
# Admitting a platform is a configuration change, never a code change (see
# `register_client`).
TRUSTED_CLIENT_ORIGINS_ENV_VAR = "GARMIN_COACH_LOOP_TRUSTED_CLIENT_ORIGINS"
HOSTED_STARTUP_DRAIN_SECONDS = 35.0
RAILWAY_GIT_COMMIT_ENV_VAR = "RAILWAY_GIT_COMMIT_SHA"
MIN_HMAC_KEY_CHARACTERS = 32
IDENTITY_DB_NAME = "identity.db"
RELEASE_ID_ENV_VAR = "GARMIN_COACH_LOOP_RELEASE_ID"
RELEASE_COMMIT_ENV_VAR = "GARMIN_COACH_LOOP_RELEASE_COMMIT"
RELEASE_INSTRUCTIONS_SHA_ENV_VAR = "GARMIN_COACH_LOOP_RELEASE_INSTRUCTIONS_SHA256"
RELEASE_OPENAPI_SHA_ENV_VAR = "GARMIN_COACH_LOOP_RELEASE_OPENAPI_SHA256"
RELEASE_DOMAIN_ENV_VAR = "GARMIN_COACH_LOOP_RELEASE_GATEWAY_DOMAIN"
RELEASE_ARTIFACT_SHA_ENV_VAR = "GARMIN_COACH_LOOP_RELEASE_GATEWAY_ARTIFACT_SHA256"

FATIGUE_LEVELS = ("normal", "elevated", "severe", "unknown")

# How long the two short-lived envelopes are good for. The authorize state has to survive
# an athlete reading a consent page; the authorization code only has to survive one
# redirect and the client's immediate token call, and is kept as short as that allows
# (see `issue_access_token` for why the code's lifetime is the replay defence).
AUTHORIZE_STATE_TTL_SECONDS = 600
AUTHORIZATION_CODE_TTL_SECONDS = 60

# The browser origins `/mcp` answers to besides its own, before the operator adds any.
# claude.ai is the one first-party connector host this product is actually reached from,
# and a deployment that did not allow it would refuse the client it was built for.
MCP_ALLOWED_ORIGINS: tuple[str, ...] = ("https://claude.ai",)

# The remote callback origins any deployment will register an MCP client for. Deliberately
# a different list from `MCP_ALLOWED_ORIGINS`, and kept separate even where the hosts
# coincide: that one answers "may this browser page talk to /mcp", this one answers "may
# this address receive an athlete's authorization". A host set that served both would tie
# two unrelated decisions to one edit.
#
# The hosts of the distribution platforms this product is actually meant to be reached
# from, each documented by its vendor as where that platform's connector receives an
# authorization. Everything else is admitted by configuration once validated, which is
# what `TRUSTED_CLIENT_ORIGINS_ENV_VAR` is for.
#
# **Origins, not callback URLs, and that distinction is what makes this list workable.**
# ChatGPT issues a different callback path per connector instance
# (`/connector/oauth/<id>`, and `/connector_platform_oauth_redirect` for apps published
# before that), and a local client's loopback port is not knowable until it binds. A list
# of whole URLs would refuse both of those; a list of origins refuses neither, and still
# refuses `https://chatgpt.com.evil.example` and `https://chatgpt.com:8443`, which is the
# part that matters.
TRUSTED_CLIENT_ORIGINS: tuple[str, ...] = (
    "https://claude.ai",
    "https://claude.com",
    "https://chatgpt.com",
)


class GatewayConfigError(RuntimeError):
    """The gateway cannot start with the configuration it was given."""


class GatewayError(RuntimeError):
    """One request is refused with an exact status and a machine-readable code.

    ``detail`` is only ever this product's own text (a validation message, a store or
    delivery block). Provider bodies, request payloads and credentials never reach it.

    ``upstream_unauthorized`` marks the one refusal an MCP client can act on by itself:
    the provider rejected this athlete's credential, so re-authorizing is the fix rather
    than a retry. The REST entry reports it as the provider error it is; see
    ``CoachGatewayHandler._mcp_tool_call``.
    """

    def __init__(
        self,
        status: HTTPStatus | int,
        code: str,
        detail: str | None = None,
        *,
        oauth: bool = False,
        extra: dict[str, Any] | None = None,
        upstream_unauthorized: bool = False,
    ):
        super().__init__(f"{int(status)} {code}")
        self.status = int(status)
        self.code = code
        self.detail = detail
        self.oauth = oauth
        self.extra = extra or {}
        self.upstream_unauthorized = upstream_unauthorized

    def payload(self) -> dict[str, Any]:
        if self.oauth:
            # RFC 6749 shape. `error` carries the whole contract, and almost every
            # refusal stops there: describing why an authorization or a token exchange
            # failed would describe the athlete's provider account or the state of a
            # flow the caller is not in. The exception is a refusal only a *person* can
            # fix -- registration against an origin this deployment does not trust --
            # where `error_description` is what turns a dead end into an instruction.
            #
            # One field, named here rather than splatting `extra`: every other use of
            # `extra` in this file carries validation reports, plan ids and delivery
            # state, and an OAuth body is not where any of those belong. A future caller
            # that passes one to an OAuth refusal drops it here instead of leaking it.
            description = self.extra.get("error_description")
            return {"error": self.code, **({"error_description": description} if description else {})}
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
    # Browser origins this deployment answers `/mcp` from, on top of `MCP_ALLOWED_ORIGINS`
    # and the request's own origin. Normalized at startup so the per-request check is a
    # set membership and never a parse.
    allowed_mcp_origins: tuple[str, ...] = ()
    # Remote callback origins this deployment will register an MCP client for, on top of
    # `TRUSTED_CLIENT_ORIGINS`. Normalized at startup for the same reason.
    trusted_client_origins: tuple[str, ...] = ()
    release_identity: dict[str, str] | None = None
    deployment_identity: dict[str, str] | None = None
    deployed_git_commit: str | None = None
    startup_drain_seconds: float = 0.0

    @property
    def identity_db_path(self) -> Path:
        return identity_db_path(self.state_root)


def identity_db_path(state_root: Path | str) -> Path:
    """Where one state root keeps its owner registry.

    Named once so an operator command can find the same file the server writes, without
    either of them holding a second copy of the layout.
    """
    return Path(state_root) / IDENTITY_DB_NAME


def gateway_artifact_sha256() -> str:
    """Digest all executed product source, not only this HTTP module."""
    package = Path(__file__).parent
    return package_artifact_sha256([(path.relative_to(package).as_posix(), path.read_bytes()) for path in package.glob("*.py")])


def _resolve_host(explicit: str | None, source: dict[str, str]) -> str:
    """The bind address: an explicit CLI value, then the environment, then loopback.

    A hosting platform needs ``0.0.0.0`` and has no CLI flag to pass, so it sets
    ``GARMIN_COACH_LOOP_GATEWAY_HOST`` instead; a local operator's explicit ``--host``
    still wins when given, exactly as before this fallback existed.
    """
    if explicit:
        return explicit
    return str(source.get(HOST_ENV_VAR) or "").strip() or DEFAULT_HOST


def _resolve_port(explicit: int | None, source: dict[str, str]) -> int:
    """The port to bind: an explicit CLI value, then the environment, then the default.

    A value that cannot become a valid TCP port is refused the same way every other
    setting in ``load_config`` is -- named, not guessed past -- so a typo'd
    platform-injected port is a refused startup rather than a socket bound to whatever
    ``int()`` happened to produce.
    """
    if explicit is not None:
        raw: Any = explicit
        origin = "--port"
    else:
        raw = str(source.get(PORT_ENV_VAR) or "").strip()
        if not raw:
            return DEFAULT_PORT
        origin = PORT_ENV_VAR
    try:
        port = int(raw)
    except (TypeError, ValueError) as exc:
        raise GatewayConfigError(f"{origin} must be an integer") from exc
    if not 0 < port < 65536:
        raise GatewayConfigError(f"{origin} must be a TCP port between 1 and 65535")
    return port


def _resolve_origin_list(source: dict[str, str], variable: str) -> tuple[str, ...]:
    """One operator-configured origin list, normalized once at startup.

    Comma-separated, and an entry that is not a bare ``scheme://host[:port]`` refuses the
    startup rather than being dropped. An origin allowlist that silently ignored the one
    typo'd entry would be a deployment that looks configured and refuses the client it was
    configured for. The error names the variable and not its value, exactly as the
    required settings above do.
    """
    raw = str(source.get(variable) or "").strip()
    if not raw:
        return ()
    entries = [part.strip() for part in raw.split(",") if part.strip()]
    origins = [_origin(entry) for entry in entries]
    if not entries or any(origin is None for origin in origins):
        raise GatewayConfigError(
            f"{variable} must be a comma-separated list of scheme://host[:port]"
        )
    return tuple(dict.fromkeys(str(origin) for origin in origins))


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

    ``host``/``port`` follow ``_resolve_host``/``_resolve_port``: an explicit value from
    the CLI wins, then ``GARMIN_COACH_LOOP_GATEWAY_HOST``/``_PORT``, then the loopback
    default -- unlike the required variables above, a platform that sets neither is not
    refused, since the loopback default remains a valid, if unreachable-from-outside,
    answer.
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
    raw_release = {
        "release_id": source.get(RELEASE_ID_ENV_VAR, ""),
        "git_commit": source.get(RELEASE_COMMIT_ENV_VAR, ""),
        "instructions_sha256": source.get(RELEASE_INSTRUCTIONS_SHA_ENV_VAR, ""),
        "openapi_sha256": source.get(RELEASE_OPENAPI_SHA_ENV_VAR, ""),
        "gateway_domain": source.get(RELEASE_DOMAIN_ENV_VAR, ""),
        "gateway_artifact_sha256": source.get(RELEASE_ARTIFACT_SHA_ENV_VAR, ""),
    }
    present_release = [bool(str(value).strip()) for value in raw_release.values()]
    if any(present_release) and not all(present_release):
        raise GatewayConfigError("gateway runtime release identity is incomplete")
    try:
        identity = release_identity(raw_release) if all(present_release) else None
    except ReleaseIdentityError as exc:
        raise GatewayConfigError(f"gateway runtime release identity is invalid: {exc}") from exc
    raw_deployment = {
        "environment": source.get(DEPLOYMENT_ENVIRONMENT_ENV_VAR, ""),
        "instance_id": source.get(DEPLOYMENT_INSTANCE_ID_ENV_VAR, ""),
    }
    present_deployment = [
        bool(str(value).strip()) for value in raw_deployment.values()
    ]
    if any(present_deployment) and not all(present_deployment):
        raise GatewayConfigError("gateway runtime deployment identity is incomplete")
    if identity is not None and not all(present_deployment):
        raise GatewayConfigError(
            "gateway runtime deployment identity is required in release mode; set "
            f"{DEPLOYMENT_ENVIRONMENT_ENV_VAR}, {DEPLOYMENT_INSTANCE_ID_ENV_VAR}"
        )
    try:
        deployment = (
            make_deployment_identity(
                resolved_state_root=state_root,
                intervals_client_id=str(source[CLIENT_ID_ENV_VAR]).strip(),
                environment=str(raw_deployment["environment"]),
                instance_id=str(raw_deployment["instance_id"]),
                token_hmac_key=key.encode("utf-8"),
            )
            if all(present_deployment)
            else None
        )
    except ReleaseIdentityError as exc:
        raise GatewayConfigError(
            f"gateway runtime deployment identity is invalid: {exc}"
        ) from exc
    deployed_git_commit = str(source.get(RAILWAY_GIT_COMMIT_ENV_VAR) or "").strip()
    if deployed_git_commit and not re.fullmatch(r"[0-9a-f]{40}", deployed_git_commit):
        raise GatewayConfigError(
            f"{RAILWAY_GIT_COMMIT_ENV_VAR} must be a lowercase 40-character Git commit"
        )
    return GatewayConfig(
        state_root=state_root,
        token_hmac_key=key.encode("utf-8"),
        intervals_client_id=str(source[CLIENT_ID_ENV_VAR]).strip(),
        intervals_client_secret=str(source[CLIENT_SECRET_ENV_VAR]).strip(),
        host=_resolve_host(host, source),
        port=_resolve_port(port, source),
        allowed_mcp_origins=_resolve_origin_list(source, MCP_ORIGINS_ENV_VAR),
        trusted_client_origins=_resolve_origin_list(
            source, TRUSTED_CLIENT_ORIGINS_ENV_VAR
        ),
        release_identity=identity,
        deployment_identity=deployment,
        deployed_git_commit=deployed_git_commit or None,
        # A platform may briefly overlap old and new processes during a nominally
        # single-replica rolling deploy. Wait past the configured 30-second drain/kill
        # window before treating any predecessor's owner lock as abandoned. Local
        # development has no deployment identity and therefore starts immediately.
        startup_drain_seconds=(
            HOSTED_STARTUP_DRAIN_SECONDS if deployment is not None else 0.0
        ),
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


def _timezone_override(body: dict[str, Any]) -> str | None:
    """A timezone this one request states, or ``None`` when it states none.

    Absent is not the default: it means "use what this athlete already told us", which
    only the owner's own stored profile can answer. Handing back a default here is how
    a hosted athlete came to live in somebody else's day.
    """
    value = body.get("timezone")
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise _invalid("timezone must be a non-empty string")
    return value


def _profile_unknowns(profile: dict[str, Any] | None) -> list[str]:
    """What this athlete has not said about themselves that a first plan depends on.

    Only the timezone. It decides which day "today" is and therefore which dates a first
    28 days land on, and nothing in the request or the provider can supply it. Language
    is not here: a plan the athlete cannot read is visibly wrong the moment they see the
    preview, while a timezone that is quietly wrong looks right until a session is a day
    out.

    Stated as an unknown rather than enforced as a requirement. An unknown is what the
    rest of the initialization already uses for a fact nobody measured, and the coach
    reads them all the same way.
    """
    if athlete_evidence.profile_timezone(profile):
        return []
    return [
        "athlete_profile.timezone is not stated; dates are being read in "
        f"{DEFAULT_TIMEZONE}"
    ]


def _only_fields(body: dict[str, Any], allowed: tuple[str, ...]) -> None:
    """Refuse a body key this route was never taught.

    The athlete-evidence routes store what the model says the athlete said, so a
    misspelled key that was quietly ignored would report success for a statement nothing
    kept. Naming the surplus key is the whole fix.
    """
    unexpected = sorted(set(body) - set(allowed))
    if unexpected:
        raise _invalid(f"unexpected field(s): {', '.join(unexpected)}")


def _context_request(body: dict[str, Any], *, timezone_name: str) -> ContextRequest:
    """Map the athlete-input half of a session request onto the existing ContextRequest.

    Omission stays unknown throughout: no available day list means availability is not
    confirmed, no red flag value means unassessed. Neither becomes a convenient default.

    ``timezone_name`` is already resolved by the caller against this owner's stored
    profile, because this function has no owner to resolve it for.
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


def normalize_scope_names(raw: Any) -> tuple[str, ...]:
    """Keep only canonical OAuth scope names, never arbitrary provider response text."""
    if not isinstance(raw, str):
        return ()
    names = {part for part in re.split(r"[\s,]+", raw.strip()) if _SCOPE_NAME.fullmatch(part)}
    return tuple(sorted(names))


def _utc_iso(moment: dt.datetime) -> str:
    return (
        moment.astimezone(dt.timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


# --------------------------------------------------------------------------------------
# OAuth request shapes. Everything here refuses rather than repairs: an authorization
# request is the one place where guessing what the client meant hands somebody a token.
# --------------------------------------------------------------------------------------


# The three spellings of "this machine", and the only hosts a plaintext callback may
# name. Everything else has to be `https`, so a code cannot travel a network in the clear.
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})


def _redirect_uri(raw: Any) -> str | None:
    """The one shape of callback this gateway will send an athlete to, or ``None``.

    Only ``http`` and ``https`` are accepted. This value is later sent as a ``Location``
    header, and a scheme like ``javascript:`` or ``data:`` reaching a browser from an
    authorization endpoint is a redirect that executes; a native client with a custom
    scheme is refused rather than accommodated by loosening this. A fragment is refused
    for the same reason the code is never put in one -- the part of a URL a browser keeps
    to itself is not somewhere an authorization response can be checked.

    ``http`` narrows further, to the loopback hosts. A local MCP client receives its code
    on ``127.0.0.1`` and cannot hold a certificate for it, which is the whole reason
    plaintext is allowed here; a plaintext callback on any other host is a code crossing
    a network unencrypted, and no client needs that.

    The caller decides what a refusal is called: registration answers RFC 7591's
    ``invalid_redirect_uri`` and the authorization endpoint answers ``invalid_request``.
    """
    value = raw.strip() if isinstance(raw, str) else ""
    if not value:
        return None
    try:
        parsed = urllib.parse.urlsplit(value)
    except ValueError:
        return None
    if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.fragment:
        return None
    if parsed.scheme == "http" and (parsed.hostname or "") not in _LOOPBACK_HOSTS:
        return None
    return value


def _is_loopback(parsed: urllib.parse.SplitResult) -> bool:
    return parsed.scheme == "http" and (parsed.hostname or "") in _LOOPBACK_HOSTS


def _registrable(redirect_uri: str, trusted: frozenset[str]) -> bool:
    """May an MCP client be registered to receive an athlete's code at this callback?

    ``_redirect_uri`` has already said the URI is a well-formed web callback. This is the
    separate question of *whose* callback it is, and it is the one PKCE cannot answer: the
    attacker who registers their own address is the client that starts the flow, so they
    hold the verifier and the code redeems for them. Intervals can tell the athlete which
    upstream application is asking; it cannot tell them which downstream client receives
    the Coach authorization that comes back. This list is where that is decided instead.

    Loopback is always registrable and needs no operator action: a local MCP client
    receives its code on the athlete's own machine, so the address is not somewhere a
    code can travel to a stranger, and its port is not knowable in advance (RFC 8252
    §7.3). The test is the *host*, not the scheme -- a client that does hold a
    certificate for its own loopback is no less local for using it, and requiring its
    origin to be configured would pin the ephemeral port it cannot promise. ``http``
    is already confined to these hosts by ``_redirect_uri``.

    Every remote callback has to name a trusted origin -- scheme, host and port,
    compared exactly, so a lookalike host or a different port is a different origin.
    """
    parsed = urllib.parse.urlsplit(redirect_uri)
    if (parsed.hostname or "") in _LOOPBACK_HOSTS:
        return True
    origin = security_log.redirect_origin(redirect_uri)
    return origin is not None and origin in trusted


# What a refused registration is told, keyed by which check refused it. Both are policy
# statements a person can act on -- the shape a callback must have, or the fact that trust
# is a deployment setting -- and neither repeats anything from the request.
_REGISTRATION_REFUSALS: dict[str, str] = {
    security_log.INVALID_REDIRECT_URI: (
        "each redirect_uri must be an https URL, or an http URL on 127.0.0.1, [::1] or "
        "localhost, and must not carry a fragment"
    ),
    security_log.UNTRUSTED_REDIRECT_ORIGIN: (
        "this deployment registers remote callbacks only on origins it trusts; a local "
        "client may use a loopback callback instead, and a hosted client's origin has to "
        "be added by the operator"
    ),
}


def _redirect_uri_matches(requested: str, registered: str) -> bool:
    """Does this authorize request name a callback its client actually registered?

    Exact string equality, with one exception the specifications require (RFC 8252 §7.3):
    a local client binds an ephemeral port at the moment it starts listening, so the port
    in its loopback callback is not knowable when it registers. For a loopback ``http``
    URI the port is therefore compared out, and scheme, host, path and query still have to
    agree. Every other URI matches exactly, port included -- a "same host, different port"
    allowance on a public domain would let a service on one port claim another's codes.
    """
    if requested == registered:
        return True
    try:
        wanted = urllib.parse.urlsplit(requested)
        known = urllib.parse.urlsplit(registered)
    except ValueError:
        return False
    if not _is_loopback(wanted) or not _is_loopback(known):
        return False
    return (wanted.hostname, wanted.path, wanted.query) == (
        known.hostname,
        known.path,
        known.query,
    )


def _client_redirect_uri(raw: Any) -> str:
    """``_redirect_uri`` as the authorization endpoint states its refusal."""
    value = _redirect_uri(raw)
    if value is None:
        raise GatewayError(HTTPStatus.BAD_REQUEST, "invalid_request", oauth=True)
    return value


def _with_query(url: str, params: dict[str, str]) -> str:
    """Append parameters to a URL that may already carry some of its own."""
    parts = urllib.parse.urlsplit(url)
    query = urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
    query.extend(params.items())
    return urllib.parse.urlunsplit(parts._replace(query=urllib.parse.urlencode(query)))


def _client_redirect(redirect_uri: str, params: dict[str, str], client_state: str) -> str:
    """Where to send the client back to, with its own ``state`` returned untouched.

    An omitted ``state`` is not echoed as an empty one: a client that sent none reads a
    ``state`` parameter as somebody else's request.
    """
    return _with_query(
        redirect_uri, {**params, **({"state": client_state} if client_state else {})}
    )


def _intervals_scope(requested: Any) -> str:
    """What to ask Intervals for, in the form Intervals reads it.

    The client's own scope request is honoured, but not verbatim: RFC 6749 delimits
    scopes with spaces and Intervals delimits them with commas, so a client that built
    its request from ``scopes_supported`` correctly would otherwise be authorized for
    nothing. ``normalize_scope_names`` accepts either and drops anything that is not a
    scope name, and an empty result falls back to the four this product declares.

    **Narrowing only.** The request is intersected with those four rather than forwarded:
    a client may ask for less than this product needs -- and then finds out at the first
    call it cannot make -- but it may not ask the athlete to grant more than the coach
    was built to use. Whether the provider would have refused a wider scope anyway is the
    provider's configuration, not this gateway's guarantee, and the athlete's consent
    screen is not the place to discover the difference.
    """
    names = tuple(
        name for name in normalize_scope_names(requested) if name in INTERVALS_OAUTH_SCOPES
    )
    return ",".join(names or INTERVALS_OAUTH_SCOPES)


def _narrowed_scope_query(raw_query: str) -> str:
    """One forwarded authorize query with its ``scope`` held to the declared set.

    Every other parameter is passed through untouched and in order: this hop belongs to
    the provider's own flow, and rewriting a parameter it signed or echoes would break it.
    A request carrying no scope at all is left carrying none -- Intervals then applies the
    application's own default, which is what it did before this narrowing existed.
    """
    if not raw_query:
        return ""
    pairs = urllib.parse.parse_qsl(raw_query, keep_blank_values=True)
    narrowed = [
        (key, _intervals_scope(value) if key == "scope" else value) for key, value in pairs
    ]
    return "?" + urllib.parse.urlencode(narrowed)


def _pkce_verified(verifier: Any, challenge: Any) -> bool:
    """RFC 7636 S256: does this verifier hash to the challenge sent at authorize time?

    Base64url without padding, and ``compare_digest`` rather than ``==``, so a wrong
    verifier is refused without the comparison itself saying how nearly it matched.
    """
    if not isinstance(verifier, str) or not verifier or not isinstance(challenge, str):
        return False
    digest = hashlib.sha256(verifier.encode("ascii", errors="ignore")).digest()
    computed = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
    return hmac.compare_digest(computed, challenge)


def _validation_summary(report: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(report, dict):
        return None
    return {
        "status": report.get("status"),
        "errors": list(report.get("errors") or []),
        "warnings": list(report.get("warnings") or []),
    }


def _unresolved_delivery_view(state_dir: Path) -> dict[str, Any] | None:
    """The delivery reservation this store is still holding, if any.

    A new conversation has no memory of the run that left it, so the state has to say so
    itself: without this the only way to learn that Intervals may hold a workout the plan
    does not describe is to attempt a plan change and be refused.

    Reported for *any* open reservation, not only one with outstanding provider effects.
    The fence is keyed on the reservation existing, so a run that died between opening it
    and its first write blocks every PlanState write exactly as hard as one that died
    mid-write -- and a conversation told nothing about it cannot act on either (issue
    #16). ``provider_effects_outstanding`` is what separates the two cases.

    Everything here is this product's own bookkeeping: no provider payload, no
    credential, no path, no owner id.
    """
    attempt = pending_delivery_attempt(state_dir)
    return None if attempt is None else _attempt_view(attempt)


def _attempt_view(attempt: dict[str, Any]) -> dict[str, Any]:
    outstanding = unresolved_delivery_operations(attempt)
    return {
        "attempt_id": attempt["attempt_id"],
        "kind": attempt["kind"],
        "opened_at": attempt["opened_at"],
        "plan_id": attempt["plan_id"],
        "plan_version": attempt["plan_version"],
        "session_ids": list(attempt["session_ids"]),
        "provider_effects_outstanding": bool(outstanding),
        "operations": [
            {
                "session_id": operation["session_id"],
                "operation": operation["operation"],
                "state": operation["state"],
                "external_id": operation["external_id"],
            }
            for operation in outstanding
        ],
        # Both are always allowed; which one is *available* depends on whether the
        # conversation still holds the confirmed set, which only the caller knows.
        "next_actions": ["retry_same_set", "clear_delivery_attempt"],
    }


def _deferred_reconciliation(unresolved: dict[str, Any]) -> dict[str, Any]:
    """Reconciliation deliberately not run, because running it would write.

    Every reconciliation entry is an ``apply_decision`` commit, and PlanState writes are
    fenced while a delivery reservation is open. Attempting one anyway turned the *read*
    path into a refusal for the one athlete who most needed to read their state: the
    session failed outright whenever a matched actual happened to be waiting.

    So the write is skipped and the omission is stated. It is not reported as "nothing to
    reconcile": no proposal was computed, so this response knows of no completed session
    and claims none (AGENTS.md 3).
    """
    return {
        "status": "deferred",
        "applied": [],
        "reason": "unresolved_delivery_attempt",
        "attempt_id": unresolved["attempt_id"],
        "detail": (
            "planned-versus-actual reconciliation writes PlanState, which is fenced "
            "while this delivery reservation is open; a session that was trained may "
            "therefore still read as planned until the reservation is resolved"
        ),
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
            # An event a confirmed change left on the calendar. Reported because the
            # session reads as not_published while Intervals still shows the athlete the
            # workout it superseded (issue #113).
            "superseded_external_id": (session.get("execution") or {}).get(
                "superseded_external_id"
            ),
        }
        for session in (plan.get("week") or {}).get("sessions", [])
        if isinstance(session, dict)
    ]
    return {"max_delivery_state": MAX_DELIVERY_STATE, "sessions": sessions}


def _decision_claims(
    *,
    owner: str,
    context: dict[str, Any],
    plan_id: str,
    base_version: int,
    after_plan: dict[str, Any],
    decision_event: dict[str, Any],
    confirmation_required: bool,
) -> dict[str, Any]:
    """State everything one confirmation of a plan change actually represents.

    Each claim is a thing that must not move between preview and commit: the athlete it
    was issued to, the evidence they were reasoning from, the plan version it was computed
    against, and the exact two artifacts it projected to. Binding the plan and event alone
    left the server able to validate a *different* context at apply time and unable to
    prove it was the one the athlete saw.

    The base version is in here too, so a proposal prepared against v3 can never be
    replayed onto v4 even when its plan and event bytes are unchanged.
    """
    context_id = context.get("context_id")
    return {
        "kind": "decision",
        "owner": owner,
        "context_id": context_id if isinstance(context_id, str) else None,
        "context_hash": canonical_hash(context),
        "plan_id": plan_id,
        "base_version": base_version,
        "after_hash": canonical_hash(after_plan),
        "event_hash": canonical_hash(decision_event),
        "confirmation_required": confirmation_required,
    }


def _initialization_claims(*, owner: str, initial_plan: dict[str, Any]) -> dict[str, Any]:
    """Bind a first plan to the athlete it is for and to the exact bytes previewed.

    A first plan has no base version and no context to bind to -- there is nothing before
    it -- so its own content and its owner are the whole binding. ``kind`` keeps it from
    ever being read as a decision proposal.
    """
    return {
        "kind": "initialization",
        "owner": owner,
        "plan_hash": canonical_hash(initial_plan),
    }


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
        """Data-free runtime readiness: bound code and deployment configuration.

        No provider call, state read or owner resolution occurs, so a readiness check can
        never be the thing that creates or touches somebody's store.
        """
        source_commit_matches = bool(
            self.config.deployed_git_commit is None
            or (
                self.config.release_identity
                and self.config.deployed_git_commit
                == self.config.release_identity["git_commit"]
            )
        )
        ready = bool(
            self.config.release_identity
            and self.config.deployment_identity
            and self.config.release_identity["gateway_artifact_sha256"]
            == gateway_artifact_sha256()
            and source_commit_matches
        )
        return {
            "status": "ok" if ready else "blocked",
            "api_version": API_VERSION,
            "release_identity": self.config.release_identity,
            "deployment_identity": self.config.deployment_identity,
            "source_git_commit": self.config.deployed_git_commit,
            "error": (
                None
                if ready
                else "missing_or_mismatched_runtime_release_deployment_or_source_identity"
            ),
        }

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
            "initialization_prepare": self.prepare_initialization,
            "initialization_apply": self.apply_initialization,
            "decision_prepare": self.prepare_decision,
            "decision_apply": self.apply_decision_request,
            "delivery_prepare": self.prepare_delivery,
            "delivery_publish": self.publish_delivery,
            "withdrawal_prepare": self.prepare_withdrawal,
            "withdrawal_apply": self.apply_withdrawal,
            "delivery_attempt_clear": self.clear_delivery_attempt,
            "permissions": self.permission_diagnostic,
            "profile_record": self.record_athlete_profile,
            "availability_record": self.record_availability,
            "strength_report": self.record_strength_report,
            "strength_prescribed_confirm": self.confirm_prescribed_strength,
        }
        try:
            return handlers[kind](owner_id, token, body)
        except GatewayError:
            raise
        except AthleteEvidenceError as exc:
            # A statement the athlete cannot have made -- an unknown weekday, a week that
            # already began, a set with no number. The fix is to ask them again, so it is
            # reported as a malformed request rather than a conflict with stored state.
            raise _invalid(str(exc)) from exc
        except ChangeRequestError as exc:
            # A coaching request -- initialization or change -- that cannot be projected
            # at all: something the model must fix, not a conflict with stored state.
            raise _invalid(str(exc)) from exc
        except StateStoreError as exc:
            raise GatewayError(HTTPStatus.CONFLICT, "state_conflict", str(exc)) from exc
        except DeliveryError as exc:
            raise GatewayError(HTTPStatus.CONFLICT, "delivery_blocked", str(exc)) from exc
        except ContextBuildError as exc:
            # A revoked or rejected token lands here. It is reported as an explicit
            # failure and nothing in the store is read differently, cleared, or defaulted
            # (AGENTS.md invariant 3). Whether the provider refused the *credential* is
            # carried along, because that is the one failure the caller can fix.
            raise GatewayError(
                HTTPStatus.BAD_GATEWAY,
                "provider_error",
                str(exc),
                upstream_unauthorized=getattr(exc, "upstream_status", None)
                in {HTTPStatus.UNAUTHORIZED, HTTPStatus.FORBIDDEN},
            ) from exc
        except IdentityError as exc:
            raise GatewayError(HTTPStatus.INTERNAL_SERVER_ERROR, "internal_error") from exc

    def _state_dir(self, owner_id: str) -> Path:
        return resolve_state_dir(owner_id, state_root=self.config.state_root)

    def _credentials(self, token: str) -> IntervalsCredentials:
        return IntervalsCredentials(token, OAUTH_ATHLETE_ID, "bearer")

    def _envelope(self) -> dict[str, Any]:
        return {"api_version": API_VERSION, "generated_at": _utc_iso(self._now())}

    def _instant(self) -> dt.datetime:
        """This request's instant, at the resolution a proposal records it.

        Truncated here rather than inside the projection so that preparing and applying
        agree on one timestamp: the candidate event carries it, and the proposal binds it.
        """
        return self._now().astimezone(dt.timezone.utc).replace(microsecond=0)

    def _unix_now(self) -> int:
        """This instant in whole unix seconds, which is how an envelope states its age."""
        return int(self._now().timestamp())

    def _settings(
        self, owner_id: str, body: dict[str, Any] | None = None
    ) -> tuple[str, str]:
        """The timezone and language this request runs under, for this owner.

        One read of the owner's own stored profile, with this request's ``timezone`` --
        when its schema carries one and it was sent -- standing in front of it for this
        call only. Every coach route goes through here, so "today" means the same day on
        all of them. ``body`` is omitted on the routes whose schema documents no
        timezone, so an undocumented key on one of those cannot quietly move the
        athlete's day.
        """
        return athlete_evidence.resolve_settings(
            self._state_dir(owner_id),
            timezone_override=_timezone_override(body) if body is not None else None,
        )

    def _local_day(self, owner_id: str, body: dict[str, Any]) -> str:
        """The athlete's own day, never the server's.

        A withdrawal refuses to touch a session whose day has already passed, so which
        day it is has to be the athlete's -- their stored profile says so, and this
        request may override it for this call.
        """
        zone_name, _ = self._settings(owner_id, body)
        try:
            zone = ZoneInfo(zone_name)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise _invalid(f"unknown timezone: {zone_name!r}") from exc
        return self._now().astimezone(zone).date().isoformat()

    def _owner_binding(self, owner_id: str) -> str:
        return binding(owner_id, key=self.config.token_hmac_key)

    def _issue_proposal(self, claims: dict[str, Any], *, now: dt.datetime) -> dict[str, Any]:
        return issue_proposal(claims, key=self.config.token_hmac_key, now=now)

    def _open_proposal(self, proposal: str, *, owner_id: str, kind: str) -> dict[str, Any]:
        """Verify one proposal belongs to this gateway, this route, and this athlete.

        Expiry is deliberately not refused here: whether a stale confirmation is a refusal
        or an already-finished write depends on what the store holds, which only the route
        can see.
        """
        try:
            opened = open_proposal(proposal, key=self.config.token_hmac_key, now=self._now())
        except ProposalError as exc:
            raise GatewayError(HTTPStatus.CONFLICT, "proposal_mismatch", str(exc)) from exc
        claims = opened["claims"]
        if claims.get("kind") != kind or claims.get("owner") != self._owner_binding(owner_id):
            # A proposal issued for another route, or to another athlete, confirms nothing
            # here -- even when the plan ids happen to match.
            raise GatewayError(HTTPStatus.CONFLICT, "proposal_mismatch")
        return opened

    @staticmethod
    def _issued_at(claims: dict[str, Any]) -> dt.datetime:
        """The instant the proposal was issued, which the same projection must reuse."""
        try:
            return dt.datetime.fromisoformat(str(claims.get("issued_at")).replace("Z", "+00:00"))
        except ValueError as exc:  # pragma: no cover - the signature already proves this
            raise GatewayError(HTTPStatus.CONFLICT, "proposal_mismatch") from exc

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

        redeemed = self._redeem_intervals_code(code)
        return {
            "token_type": "Bearer",
            "access_token": redeemed["access_token"],
            "scope": ",".join(redeemed["scope_names"]),
        }

    def _redeem_intervals_code(self, code: str) -> dict[str, Any]:
        """Trade one Intervals code for a token and remember only who it belongs to.

        The one place a provider code becomes a provider token, shared by both token
        endpoints so there is a single definition of "this athlete has connected":
        exchange with the server-held secret, refuse anything that cannot be tied to a
        stable athlete, then record the keyed fingerprint the identity registry resolves.

        The plaintext token is returned to the caller and never stored.
        """
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
            scope_names = normalize_scope_names(payload.get("scope"))
            record_token_fingerprint(
                self.config.identity_db_path,
                token_fingerprint(access_token, hmac_key=self.config.token_hmac_key),
                owner_id,
                PROVIDER,
                scope_names=scope_names,
            )
        except IdentityError as exc:
            raise GatewayError(HTTPStatus.BAD_GATEWAY, "server_error", oauth=True) from exc

        return {
            "access_token": access_token,
            "scope_names": scope_names,
            "owner_id": owner_id,
        }

    # -- authorization server ---------------------------------------------------------
    #
    # The three routes below are this gateway acting as an authorization server of its
    # own, in front of Intervals, rather than forwarding a client to Intervals and
    # handing back whatever Intervals issued. The MCP entry needs that for four reasons
    # the passthrough could not satisfy: an MCP server must not accept a token minted for
    # another service, a token has to name the audience it is for, PKCE has to be
    # verified by somebody (Intervals implements none), and a leaked client-side token
    # must not be the athlete's Intervals credential itself.
    #
    # `/oauth/intervals/*` is untouched and remains the Custom GPT entry's contract.
    #
    # Every refusal below is recorded as it is raised, through the two helpers here. The
    # refusal the client sees stays exactly as narrow as it was -- an OAuth error code and
    # nothing else -- while what is written down says which check refused it. Both matter
    # and they are not the same audience: the client is told only what it may act on, and
    # the operator gets what an incident is reconstructed from.

    def _trusted_client_origins(self) -> frozenset[str]:
        """Which remote callback origins this deployment will register a client for."""
        return frozenset({*TRUSTED_CLIENT_ORIGINS, *self.config.trusted_client_origins})

    def _oauth_refusal(
        self,
        event: str,
        reason: str,
        error: str,
        *,
        redirect_uri: Any = None,
        client_id: Any = None,
        description: str | None = None,
    ) -> GatewayError:
        """Record one OAuth refusal and return the error to raise for it."""
        security_log.emit(
            event,
            security_log.REFUSED,
            key=self.config.token_hmac_key,
            reason=reason,
            redirect_uri=redirect_uri,
            client_id=client_id,
        )
        return GatewayError(
            HTTPStatus.BAD_REQUEST,
            error,
            oauth=True,
            extra={"error_description": description} if description else None,
        )

    def _refuse_registration(
        self, reason: str, *, redirect_uri: Any = None
    ) -> GatewayError:
        """``_oauth_refusal`` for registration, which says what a person can do next.

        RFC 7591 gives one error code for every bad callback, so a client cannot tell a
        malformed URI from a perfectly formed one on an origin this deployment does not
        trust -- and those have opposite fixes. The description is the difference, and it
        is the one OAuth refusal in this gateway that carries one: whoever reads it is a
        developer or an athlete looking at a connector that will not connect, and without
        it the only signal is a connection that fails for no stated reason.

        It states policy, never the request: the rejected URI is not echoed back, and
        nothing here varies with what was sent beyond which of the two checks refused it.
        A reason with no text written for it falls back to the bare RFC error rather than
        failing the request -- a missing sentence is not a reason to answer ``500``.
        """
        return self._oauth_refusal(
            security_log.CLIENT_REGISTRATION,
            reason,
            "invalid_redirect_uri",
            redirect_uri=redirect_uri,
            description=_REGISTRATION_REFUSALS.get(reason),
        )

    def start_authorization(self, query: dict[str, str], *, base_url: str) -> str:
        """Begin one authorization and return where to send the athlete.

        Everything the client asked for that must survive the round trip -- which client
        it is, where to come back to, its own ``state``, its PKCE challenge, the resource
        it wants a token for -- is sealed into the ``state`` this gateway sends to
        Intervals. That is what keeps the server stateless without letting the client
        rewrite its own request halfway through: the values come back inside a MAC this
        gateway alone can make.

        The requested callback has to be one this ``client_id`` registered. PKCE alone
        does not cover this: whoever *starts* a flow holds the verifier, so an attacker
        who can name an arbitrary callback under a client id anyone may present receives
        the code at their own address and redeems it themselves.

        Refusals here are plain ``400``s rather than redirects. A redirect_uri that has
        not been checked is not somewhere to send an error.
        """
        client_id = str(query.get("client_id") or "")
        if query.get("response_type") != "code":
            raise self._oauth_refusal(
                security_log.AUTHORIZATION,
                security_log.UNSUPPORTED_RESPONSE_TYPE,
                "unsupported_response_type",
                client_id=client_id,
            )
        registered = self._registered_redirect_uris(client_id)
        try:
            redirect_uri = _client_redirect_uri(query.get("redirect_uri"))
        except GatewayError:
            raise self._oauth_refusal(
                security_log.AUTHORIZATION,
                security_log.INVALID_REDIRECT_URI,
                "invalid_request",
                client_id=client_id,
            ) from None
        if not any(_redirect_uri_matches(redirect_uri, known) for known in registered):
            raise self._oauth_refusal(
                security_log.AUTHORIZATION,
                security_log.REDIRECT_NOT_REGISTERED,
                "invalid_request",
                redirect_uri=redirect_uri,
                client_id=client_id,
            )
        challenge = str(query.get("code_challenge") or "").strip()
        if not challenge or query.get("code_challenge_method") != "S256":
            # Advertised as required, and now required in fact: without a challenge there
            # is nothing binding the code to the client that asked for it.
            raise self._oauth_refusal(
                security_log.AUTHORIZATION,
                security_log.MISSING_PKCE_CHALLENGE,
                "invalid_request",
                redirect_uri=redirect_uri,
                client_id=client_id,
            )
        security_log.emit(
            security_log.AUTHORIZATION,
            security_log.ACCEPTED,
            key=self.config.token_hmac_key,
            redirect_uri=redirect_uri,
            client_id=client_id,
        )
        state = token_envelope.seal(
            {
                "client_id": client_id,
                "client_redirect_uri": redirect_uri,
                "client_state": str(query.get("state") or ""),
                "code_challenge": challenge,
                "resource": str(query.get("resource") or ""),
                "iat": self._unix_now(),
            },
            kind=token_envelope.AUTHORIZE_STATE,
            key=self.config.token_hmac_key,
        )
        return _with_query(
            INTERVALS_AUTHORIZE_URL,
            {
                "client_id": self.config.intervals_client_id,
                # Intervals redirects here, never to the client: the client's own URI is
                # in the state and is honoured on the way back out.
                "redirect_uri": f"{base_url}{CALLBACK_PATH}",
                "state": state,
                "scope": _intervals_scope(query.get("scope")),
            },
        )

    def complete_authorization(self, query: dict[str, str]) -> str:
        """Turn Intervals' answer into this gateway's own code, and return where to go.

        The provider code is redeemed here, server-side, so the client never sees a
        provider credential at any point in the flow. What it receives instead is an
        authorization code of this gateway's own: the Intervals token sealed together
        with the PKCE challenge it must later answer for.

        An upstream refusal comes back as ``access_denied`` and nothing else. Which
        provider said no, and why, is between the athlete and Intervals.
        """
        try:
            opened = token_envelope.open_envelope(
                query.get("state"),
                kind=token_envelope.AUTHORIZE_STATE,
                key=self.config.token_hmac_key,
                now=self._now(),
                max_age_seconds=AUTHORIZE_STATE_TTL_SECONDS,
            )
        except EnvelopeError as exc:
            # Without a state this gateway issued there is no client to redirect to, so
            # this is the one failure the athlete sees as a bare error.
            raise self._oauth_refusal(
                security_log.PROVIDER_CALLBACK,
                security_log.UNKNOWN_AUTHORIZE_STATE,
                "invalid_request",
            ) from exc

        redirect_uri = str(opened.get("client_redirect_uri") or "")
        client_state = str(opened.get("client_state") or "")
        client_id = str(opened.get("client_id") or "")
        code = query.get("code")
        if query.get("error") or not isinstance(code, str) or not code.strip():
            security_log.emit(
                security_log.PROVIDER_CALLBACK,
                security_log.REFUSED,
                key=self.config.token_hmac_key,
                reason=security_log.PROVIDER_DENIED,
                redirect_uri=redirect_uri,
                client_id=client_id,
            )
            return _client_redirect(redirect_uri, {"error": "access_denied"}, client_state)
        try:
            redeemed = self._redeem_intervals_code(code.strip())
        except GatewayError:
            security_log.emit(
                security_log.PROVIDER_CALLBACK,
                security_log.REFUSED,
                key=self.config.token_hmac_key,
                reason=security_log.PROVIDER_EXCHANGE_FAILED,
                redirect_uri=redirect_uri,
                client_id=client_id,
            )
            return _client_redirect(redirect_uri, {"error": "access_denied"}, client_state)

        security_log.emit(
            security_log.PROVIDER_CALLBACK,
            security_log.ACCEPTED,
            key=self.config.token_hmac_key,
            redirect_uri=redirect_uri,
            client_id=client_id,
        )
        issued = token_envelope.seal(
            {
                "intervals_token": redeemed["access_token"],
                # Carried so the token endpoint can state the granted scope without a
                # second provider call; it is the provider's own answer, normalized.
                "scope": ",".join(redeemed["scope_names"]),
                "code_challenge": opened.get("code_challenge"),
                # Which registration this code belongs to, carried the whole way so the
                # token endpoint can refuse a code redeemed under another client's id.
                "client_id": client_id,
                "client_redirect_uri": redirect_uri,
                "resource": opened.get("resource"),
                "iat": self._unix_now(),
            },
            kind=token_envelope.AUTHORIZATION_CODE,
            key=self.config.token_hmac_key,
        )
        return _client_redirect(redirect_uri, {"code": issued}, client_state)

    def issue_access_token(self, form: dict[str, str], *, base_url: str) -> dict[str, Any]:
        """Redeem this gateway's own authorization code for its own access token.

        Four things must hold, and each is checked against the code's own sealed contents
        rather than against anything remembered: the caller is the client the code was
        issued to, it holds the verifier for the challenge sent at authorize time, it is
        coming back to the redirect URI it named, and it is not quietly asking for a token
        for a different resource.

        **Single use is bought with time and PKCE, not with a database.** A stateless
        server cannot remember that a code was already redeemed. What it can do is make
        the window too short to reach (60 seconds, and the client redeems immediately)
        and make a stolen code useless without the verifier, which never leaves the
        client. The alternative -- a table of spent codes -- is the credential store this
        product deliberately does not keep.

        No refresh token and no ``expires_in``: Intervals issues neither, and its access
        tokens do not expire on a schedule. Claiming a lifetime this server cannot honour
        would have clients re-authorizing on a timer that means nothing. When the
        provider does end the credential, the athlete finds out the way MCP intends --
        a ``401`` on the next call, with the challenge that restarts this flow. Should
        Intervals ever issue refresh tokens, the upgrade is a fourth envelope kind here,
        not a change to what the client does.
        """
        presented = str(form.get("client_id") or "")
        grant_type = form.get("grant_type")
        if grant_type == "refresh_token":
            # Same answer, same reason as the Custom GPT entry's token endpoint: there is
            # no refresh token to present, so saying so plainly makes the client
            # re-authorize instead of retrying forever.
            raise self._token_refusal(
                security_log.NO_REFRESH_GRANT, "invalid_grant", client_id=presented
            )
        if grant_type != "authorization_code":
            raise self._token_refusal(
                security_log.UNSUPPORTED_GRANT_TYPE,
                "unsupported_grant_type",
                client_id=presented,
            )
        try:
            opened = token_envelope.open_envelope(
                form.get("code"),
                kind=token_envelope.AUTHORIZATION_CODE,
                key=self.config.token_hmac_key,
                now=self._now(),
                max_age_seconds=AUTHORIZATION_CODE_TTL_SECONDS,
            )
        except EnvelopeError as exc:
            raise self._token_refusal(
                security_log.INVALID_AUTHORIZATION_CODE, "invalid_grant", client_id=presented
            ) from exc
        client_id = str(opened.get("client_id") or "")
        if presented != client_id:
            # A public client authenticates with nothing, so ``client_id`` is all it can
            # present -- and presenting it is what stops a code issued to one registration
            # from being redeemed as another's. ``400`` rather than ``401``: nothing was
            # attempted in an ``Authorization`` header, so there is no challenge to reissue.
            # The event names the *code's* client, which is the flow this belongs to.
            raise self._token_refusal(
                security_log.CLIENT_MISMATCH, "invalid_client", client_id=client_id
            )
        if not _pkce_verified(form.get("code_verifier"), opened.get("code_challenge")):
            raise self._token_refusal(
                security_log.PKCE_VERIFICATION_FAILED, "invalid_grant", client_id=client_id
            )
        if form.get("redirect_uri") != opened.get("client_redirect_uri"):
            raise self._token_refusal(
                security_log.REDIRECT_MISMATCH, "invalid_grant", client_id=client_id
            )
        requested_resource = str(form.get("resource") or "")
        if requested_resource and requested_resource != str(opened.get("resource") or ""):
            raise self._token_refusal(
                security_log.RESOURCE_MISMATCH, "invalid_target", client_id=client_id
            )

        scope = str(opened.get("scope") or "")
        access_token = token_envelope.seal(
            {
                "intervals_token": opened.get("intervals_token"),
                # The audience this token may be presented to, and nowhere else. A copy
                # replayed against another deployment of this same code is refused there.
                "aud": f"{base_url}{MCP_PATH}",
                "scope": scope,
                # For the security log alone: what lets an authenticated call on `/mcp`
                # be attributed to the registration whose flow issued it. Nothing
                # authorizes on it -- the audience above is the binding.
                #
                # The handle, not the `client_id` itself. The id is a sealed registration
                # of its own and carrying it here would make every access token, sent on
                # every request for the life of the connection, most of a kilobyte to say
                # one 16-character thing.
                "client": security_log.client_fingerprint(
                    client_id, key=self.config.token_hmac_key
                ),
                "iat": self._unix_now(),
            },
            kind=token_envelope.ACCESS_TOKEN,
            key=self.config.token_hmac_key,
        )
        security_log.emit(
            security_log.TOKEN_ISSUANCE,
            security_log.ACCEPTED,
            key=self.config.token_hmac_key,
            redirect_uri=str(opened.get("client_redirect_uri") or ""),
            client_id=client_id,
        )
        return {"token_type": "Bearer", "access_token": access_token, "scope": scope}

    def _token_refusal(
        self, reason: str, error: str, *, client_id: str
    ) -> GatewayError:
        """``_oauth_refusal`` for the token endpoint, which never names a callback."""
        return self._oauth_refusal(
            security_log.TOKEN_ISSUANCE, reason, error, client_id=client_id
        )

    def resolve_mcp_owner(self, token: str | None, *, base_url: str | None) -> tuple[str, str]:
        """Resolve one MCP bearer to ``(owner, provider credential)``, or refuse.

        The bearer here is only ever an envelope this gateway sealed. A bare Intervals
        token presented on ``/mcp`` is refused exactly like any other unopenable value:
        the athlete's provider credential is not an identity this entry accepts, which is
        the whole point of the change.

        The provider credential comes back out for the route handlers, which need it as
        what it is -- the credential for this athlete's own Intervals calls.
        """
        if token is None or base_url is None:
            raise self._mcp_refusal(security_log.MISSING_BEARER)
        try:
            opened = token_envelope.open_envelope(
                token,
                kind=token_envelope.ACCESS_TOKEN,
                key=self.config.token_hmac_key,
                now=self._now(),
                max_age_seconds=None,
            )
        except EnvelopeError as exc:
            raise self._mcp_refusal(security_log.UNRECOGNIZED_TOKEN) from exc
        # The handle the token endpoint put here, not a client id: see `issue_access_token`
        # for why the id itself does not travel in a token sent on every request.
        client = str(opened.get("client") or "")
        audience = str(opened.get("aud") or "")
        if audience.casefold() != f"{base_url}{MCP_PATH}".casefold():
            raise self._mcp_refusal(security_log.AUDIENCE_MISMATCH, client=client)
        provider_token = opened.get("intervals_token")
        if not isinstance(provider_token, str) or not provider_token:
            raise self._mcp_refusal(security_log.UNRECOGNIZED_TOKEN, client=client)
        # The identity registry stays the single answer to "whose store is this": an
        # envelope for a token that has since stopped being recognised resolves to
        # nothing, exactly as a bare token would have.
        try:
            owner_id = self.resolve_owner(provider_token)
        except GatewayError:
            raise self._mcp_refusal(security_log.UNKNOWN_OWNER, client=client) from None
        # A revocation the athlete asked for has to outlive the credential behind it.
        # Deleting the fingerprints alone is not enough: this token carries the provider
        # credential rather than a fingerprint, so recording that credential again -- the
        # athlete reconnecting on a token Intervals still accepts -- would resolve it once
        # more. Every token issued before the revocation is refused here instead.
        try:
            revoked = revoked_after(self.config.identity_db_path, owner_id)
        except IdentityError:
            raise self._mcp_refusal(security_log.UNKNOWN_OWNER, client=client) from None
        issued_at = opened.get("iat")
        if revoked is not None and (
            not isinstance(issued_at, int) or isinstance(issued_at, bool) or issued_at <= revoked
        ):
            raise self._mcp_refusal(security_log.UNRECOGNIZED_TOKEN, client=client)
        # One event per authenticated request, not per connection: a stateless server has
        # no connection to attach it to, and the volume is the same order as the request
        # line already written for every call.
        security_log.emit(
            security_log.MCP_AUTHENTICATION,
            security_log.ACCEPTED,
            key=self.config.token_hmac_key,
            client_handle=client,
        )
        return owner_id, provider_token

    def _mcp_refusal(self, reason: str, *, client: str = "") -> GatewayError:
        """One refused ``/mcp`` authentication, recorded and answered as ``401``.

        Never an OAuth error body: this boundary answers with the RFC 9728 challenge that
        restarts authorization, and the reason for the refusal is written down rather than
        told to whoever presented the token.
        """
        security_log.emit(
            security_log.MCP_AUTHENTICATION,
            security_log.REFUSED,
            key=self.config.token_hmac_key,
            reason=reason,
            client_handle=client,
        )
        return GatewayError(HTTPStatus.UNAUTHORIZED, "unauthorized")

    def register_client(self, body: dict[str, Any]) -> dict[str, Any]:
        """Register one MCP client into the ``client_id`` it is handed, never into a table.

        RFC 7591 registration, answered by sealing what was registered -- the redirect
        URIs, and when -- into the id itself. The id is therefore the registration: it
        opens only under this gateway's key, so a client that presents one has proven it
        received it here, and the URIs it may come back to arrive with it. That keeps the
        server stateless in the way the rest of this product is stateless (see
        ``token_envelope``): no client table to persist, share between replicas, or expire.

        It does not expire either. A registration that stopped opening after some number
        of days would take a working connector down with no failure anyone could act on,
        and there is nothing time-limited about "this client asked to come back here".

        Public, exactly: ``token_endpoint_auth_method`` is ``none`` and no secret is
        issued, because the client is a program the athlete's agent runs and could not
        keep one. The Intervals secret stays in this process, and the Intervals client id
        stays what it always was -- this gateway's own credential upstream, not something
        an MCP client may present as its identity.

        A URI that fails the scheme policy fails the whole registration rather than being
        dropped from it: a client told it is registered, whose callback silently is not,
        fails later at authorize time with nothing to connect the two.

        **Registration is not open to arbitrary remote callbacks.** A remote URI has to
        name an origin this deployment trusts (see ``_registrable``); loopback needs no
        trust decision and never did. This is where an attacker's own callback is stopped,
        and it is deliberately stopped *here* -- before an authorization can start, and so
        before the athlete is ever shown an Intervals consent screen that would not have
        named the client receiving the result. The cost is honest: an unknown hosted MCP
        client cannot connect zero-config, and a new platform is admitted by configuring
        its origin once its flow has been validated.

        Intervals never sees a client's redirect URI at all, so it validates only this
        gateway's own callback -- one URI, registered once by the operator.
        """
        submitted = body.get("redirect_uris")
        if not isinstance(submitted, list) or not submitted:
            raise self._refuse_registration(security_log.INVALID_REDIRECT_URI)
        trusted = self._trusted_client_origins()
        redirect_uris: list[str] = []
        for uri in submitted:
            checked = _redirect_uri(uri)
            if checked is None:
                raise self._refuse_registration(
                    security_log.INVALID_REDIRECT_URI,
                    # Only a value that is already a well-formed callback is worth naming
                    # an origin for; anything else is written as absent by `emit`.
                    redirect_uri=uri,
                )
            if not _registrable(checked, trusted):
                raise self._refuse_registration(
                    security_log.UNTRUSTED_REDIRECT_ORIGIN, redirect_uri=checked
                )
            redirect_uris.append(checked)
        issued_at = self._unix_now()
        client_id = token_envelope.seal(
            {"redirect_uris": redirect_uris, "iat": issued_at},
            kind=token_envelope.CLIENT_REGISTRATION,
            key=self.config.token_hmac_key,
        )
        security_log.emit(
            security_log.CLIENT_REGISTRATION,
            security_log.ACCEPTED,
            key=self.config.token_hmac_key,
            # One origin, not all of them: a client registering several callbacks
            # registers them on one origin in every case this product has seen, and the
            # event is a handle for the flow rather than a copy of the request.
            redirect_uri=redirect_uris[0],
            client_id=client_id,
        )
        registered = {
            "client_id": client_id,
            "client_id_issued_at": issued_at,
            "redirect_uris": redirect_uris,
            "token_endpoint_auth_method": "none",
            "grant_types": ["authorization_code"],
            "response_types": ["code"],
        }
        client_name = body.get("client_name")
        if isinstance(client_name, str) and client_name.strip():
            # Echoed because RFC 7591 says a registered value comes back, not because
            # anything here reads it: the name is what the client calls itself.
            registered["client_name"] = client_name
        return registered

    def _registered_redirect_uris(self, client_id: str) -> list[str]:
        """Open one ``client_id`` back into the callbacks it registered, or refuse.

        A value this gateway did not seal -- an invented id, a tampered one, the Intervals
        client id, an envelope of another kind -- opens as nothing, and nothing is what
        distinguishes a registered client here. The refusal says only
        ``unauthorized_client``, for the same reason the envelope refuses without saying
        which check failed.
        """
        try:
            opened = token_envelope.open_envelope(
                client_id,
                kind=token_envelope.CLIENT_REGISTRATION,
                key=self.config.token_hmac_key,
                now=self._now(),
                max_age_seconds=None,
            )
        except EnvelopeError as exc:
            raise self._unknown_client(client_id) from exc
        uris = opened.get("redirect_uris")
        if not isinstance(uris, list) or not uris or not all(isinstance(u, str) for u in uris):
            raise self._unknown_client(client_id)
        return uris

    def _unknown_client(self, client_id: str) -> GatewayError:
        return self._oauth_refusal(
            security_log.AUTHORIZATION,
            security_log.UNKNOWN_CLIENT,
            "unauthorized_client",
            client_id=client_id,
        )

    def permission_diagnostic(self, owner_id: str, token: str, body: dict[str, Any]) -> dict[str, Any]:
        """Classify the connected token's SETTINGS read capability without reading its body."""
        del owner_id, body
        fingerprint = token_fingerprint(token, hmac_key=self.config.token_hmac_key)
        scopes = scopes_for_fingerprint(self.config.identity_db_path, fingerprint)
        request = urllib.request.Request(
            BASE_URL.format(athlete_id=OAUTH_ATHLETE_ID) + SPORT_SETTINGS_PATH, method="GET"
        )
        request.add_header("Authorization", authorization_header(self._credentials(token)))
        request.add_header("User-Agent", USER_AGENT)
        try:
            if self.fetch is None:
                with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS):
                    pass
            else:
                self.fetch(request)
        except urllib.error.HTTPError as exc:
            if exc.code == HTTPStatus.UNAUTHORIZED:
                classification = "invalid_or_expired"
            elif exc.code == HTTPStatus.FORBIDDEN:
                classification = "denied"
            else:
                raise GatewayError(HTTPStatus.BAD_GATEWAY, "provider_error") from exc
        except urllib.error.URLError as exc:
            raise GatewayError(HTTPStatus.BAD_GATEWAY, "provider_error") from exc
        else:
            classification = "readable"
        return {
            "status": "passed",
            **self._envelope(),
            "granted_scopes": list(scopes) if scopes is not None else None,
            "settings_read": classification,
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
        timezone_name, _ = self._settings(owner_id, body)
        request = _context_request(body, timezone_name=timezone_name)
        try:
            window = build_window(request, self._now())
        except ContextBuildError as exc:
            # A bad timezone or as_of is a malformed request, not a provider outage.
            raise _invalid(str(exc)) from exc

        if not (state_dir / "store.json").is_file():
            # An empty account is a fact to report, not a store to create. Initialising a
            # plan is a coaching decision, and this transport never makes one.
            observations, unknowns = self._pre_plan_observations(state_dir, token, window)
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
                "unknowns": ["no PlanState exists for this account", *unknowns],
                "delivery": None,
                "reconciliation": None,
                "pre_plan_observations": observations,
            }

        report = self._build_context(request, state_dir, token)
        # Reading state must not depend on being allowed to write it. A reservation left
        # by an interrupted delivery fences every PlanState commit, and reconciliation is
        # made of commits, so the write is deferred rather than attempted.
        unresolved = _unresolved_delivery_view(state_dir)
        if unresolved is None:
            reconciliation = apply_reconciliation(state_dir, report["context"])
            if reconciliation["status"] != "passed":
                raise GatewayError(
                    HTTPStatus.UNPROCESSABLE_ENTITY,
                    "reconciliation_blocked",
                    extra={"reconciliation": reconciliation},
                )
            if reconciliation["applied"]:
                report = self._build_context(request, state_dir, token)
        else:
            reconciliation = _deferred_reconciliation(unresolved)

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
            "delivery": {
                **_delivery_view(plan),
                "unresolved_delivery": unresolved,
            },
            "reconciliation": reconciliation,
        }

    def _pre_plan_observations(
        self, state_dir: Path, token: str, window: BuildWindow
    ) -> tuple[dict[str, Any], list[str]]:
        """What is already known about an athlete who has no plan yet.

        Without this, the first conversation asks for everything -- including the months
        of training Intervals has been holding all along, and any availability or lift the
        athlete reported before deciding what to train. Re-asking is not neutral: it
        collects a worse answer than the record already holds, and it spends the one turn
        where the athlete is deciding whether this is worth using. What genuinely has to
        be asked is the goal, the days, and the baselines no device measures.

        The provider read is best-effort by construction. It is the only optional half:
        a failure here degrades to ``recent_training: null`` plus a stated unknown and the
        empty account is still reported (AGENTS.md 3), because an athlete who cannot see
        their history has still not lost the ability to start a plan.
        """
        unknowns: list[str] = []

        # Raises StateStoreError (-> 409) on an unreadable file: an account with evidence
        # it cannot read is not an account with no evidence.
        evidence = athlete_evidence.load_evidence(state_dir)
        recurring = (evidence.get("availability") or {}).get("recurring")
        overrides = (evidence.get("availability") or {}).get("week_overrides") or []
        reports = evidence.get("strength_reports") or []
        athlete_evidence_view: dict[str, Any] | None = None
        if recurring is not None or overrides or reports:
            athlete_evidence_view = {
                "availability": {
                    "recurring": recurring,
                    "effective_this_week": athlete_evidence.effective_availability(
                        evidence, week_start=athlete_evidence.week_start_for(window.as_of.date())
                    ),
                },
                # Whole reports, not a count: there are a handful at most before a plan
                # exists, and a count would only prompt a second call to read them.
                "strength_reports": list(reports),
            }

        recent_training: dict[str, Any] | None = None
        try:
            domain = fetch_domain(self._credentials(token), window, fetch=self.fetch)
        except ContextBuildError as exc:
            unknowns.append(f"recent_training unavailable: {exc}")
        else:
            recent_training = {
                "window_start": domain.actuals_window_start.isoformat(),
                "window_end": window.window42_end.isoformat(),
                "recent_actuals": domain.recent_actuals,
                "coverage_activities": coverage_entry(len(domain.activity_days)),
            }

        return (
            {"athlete_evidence": athlete_evidence_view, "recent_training": recent_training},
            unknowns,
        )

    # -- athlete-reported evidence ------------------------------------------------------

    def record_athlete_profile(
        self, owner_id: str, token: str, body: dict[str, Any]
    ) -> dict[str, Any]:
        """Store where this athlete is and what language their plan is written in.

        Single-step like the other evidence routes, and for the same reason: it changes
        no plan and creates no DecisionEvent, so asking the athlete to confirm what they
        just said would cost a turn and buy nothing.

        Either field alone is a complete call. "I'm in Berlin" says nothing about which
        language they read, and sending a language the athlete never mentioned to keep
        the shape tidy would store a guess as their own statement.
        """
        _only_fields(body, ("timezone", "language"))
        return {
            "status": "passed",
            **self._envelope(),
            **athlete_evidence.record_profile(
                self._state_dir(owner_id),
                timezone=body.get("timezone"),
                language=body.get("language"),
                now=self._now(),
            ),
        }

    def record_availability(
        self, owner_id: str, token: str, body: dict[str, Any]
    ) -> dict[str, Any]:
        """Store which weekdays this athlete can train (issue #28).

        Single-step on purpose, unlike every plan write on this gateway. Prepare/apply
        exists so a coaching decision is previewed and confirmed before it changes the
        athlete's plan; this changes no plan, creates no DecisionEvent, and touches no
        PlanState version. It records a statement the athlete just made, and asking them
        to confirm what they said one message ago buys nothing and costs a turn.

        ``week`` layers onto ``recurring`` rather than replacing it, so one sentence
        stays one call: with Mon/Wed/Fri standing, "something came up Wednesday" is
        ``week: {unavailable_days: ["wed"]}`` and the response already says Mon and Fri
        are what is left. Nothing here asks the caller which kind of write it is
        performing -- the shape of the athlete's sentence picks the field, and
        ``effective_this_week`` is the answer to read back to them.
        """
        _only_fields(body, ("timezone", "recurring", "week"))
        recurring = body.get("recurring")
        week = body.get("week")
        if recurring is None and week is None:
            raise _invalid("recurring, week, or both are required")
        return {
            "status": "passed",
            **self._envelope(),
            **athlete_evidence.record_availability(
                self._state_dir(owner_id),
                recurring=recurring,
                week=week,
                timezone_name=self._settings(owner_id, body)[0],
                now=self._now(),
            ),
        }

    def record_strength_report(
        self, owner_id: str, token: str, body: dict[str, Any]
    ) -> dict[str, Any]:
        """Store what the athlete says they lifted for one movement (issue #47).

        Also single-step, and for the same reason. Only ``exercise`` and ``sets`` are
        required: the day is today unless another was named, the set numbers follow their
        order, and ``category`` is optional metadata rather than a question to put to the
        athlete. So "bench 65 by 4" is one call with no follow-up.

        A second report for the same movement on the same day replaces the first, which
        is what makes "sorry, 70 not 65" a correction rather than a second set. The
        response names the derived ``report_id``, whether this was an exact replay, and
        what it replaced, so a retried turn can tell all three apart.
        """
        _only_fields(body, ("timezone", "date", "exercise", "category", "sets", "notes"))
        return {
            "status": "passed",
            **self._envelope(),
            **athlete_evidence.record_strength_report(
                self._state_dir(owner_id),
                date=body.get("date"),
                exercise=body.get("exercise"),
                category=body.get("category"),
                sets=body.get("sets"),
                notes=body.get("notes"),
                timezone_name=self._settings(owner_id, body)[0],
                now=self._now(),
            ),
        }

    def confirm_prescribed_strength(
        self, owner_id: str, token: str, body: dict[str, Any]
    ) -> dict[str, Any]:
        """Record a prescribed strength session as done, with whatever differed (issue #76).

        Single-step for the same reason the other two are: "今天重訓照做了" is one
        statement, and the plan already holds the sets it refers to. Only ``session_id``
        is required; ``deviations`` carries the parts that differed, so "照做，但臥推最後
        一組只推了 3 下" is still one call.

        The session is read from the *current* plan rather than accepted from the caller,
        so what gets recorded is the prescription the athlete actually has, not one the
        conversation reconstructed. A session id the current plan does not hold is a
        malformed request, not a conflict: the fix is to look at the plan again.
        """
        _only_fields(body, ("timezone", "session_id", "deviations"))
        session_id = _string_field(body, "session_id")
        current = read_current_plan(self._state_dir(owner_id))
        if current is None:
            raise _invalid("there is no current plan to confirm a session against")
        sessions = (current["current_plan"].get("week") or {}).get("sessions") or []
        session = next(
            (item for item in sessions if item.get("session_id") == session_id), None
        )
        if session is None:
            raise _invalid(f"the current plan holds no session {session_id!r} this week")
        return {
            "status": "passed",
            **self._envelope(),
            "plan_id": current["current_plan"]["plan_id"],
            "plan_version": current["current_version"],
            **athlete_evidence.confirm_prescribed_strength(
                self._state_dir(owner_id),
                session=session,
                deviations=body.get("deviations"),
                timezone_name=self._settings(owner_id, body)[0],
                now=self._now(),
            ),
        }

    def prepare_initialization(
        self, owner_id: str, token: str, body: dict[str, Any]
    ) -> dict[str, Any]:
        """Project one coaching initialization request and preview it. Writes nothing.

        ``start_session`` reports an empty account and stops there, because deciding what
        an athlete's first 28 days should contain is coaching, not transport. This is the
        other half of that boundary: the request carries coaching judgment and athlete
        facts, ``project_initialization_request`` derives every mechanical field of the
        candidate plan, and only a separate confirmed call turns it into state.

        This is also where an athlete with no stored profile is asked for one. The first
        28 days are laid out on dates, and which dates those are depends on which day it
        is where the athlete lives; a hosted athlete who never said would have the
        deployment's own timezone answer for them, silently. So the response names the
        unstated timezone among the plan's other unknowns and carries ``athlete_profile``
        beside it. It is not a gate: an athlete who does not want to say still gets their
        plan (AGENTS.md 5), and the coach can see exactly what it was built on.
        """
        state_dir = self._state_dir(owner_id)
        request = _object_field(body, "initialization_request")
        self._require_no_plan_state(state_dir)
        profile = athlete_evidence.stored_profile(
            athlete_evidence.load_evidence(state_dir)
        )
        issued_at = self._instant()
        projection = project_initialization_request(
            request,
            issued_at=issued_at,
            language=athlete_evidence.profile_language(profile),
        )
        plan = projection["plan"]
        validation = self._validate_initial_plan(plan)
        issued = self._issue_proposal(
            _initialization_claims(owner=self._owner_binding(owner_id), initial_plan=plan),
            now=issued_at,
        )
        return {
            "status": "passed",
            **self._envelope(),
            "plan_id": plan["plan_id"],
            "plan_version": plan["version"],
            "proposal": issued["proposal"],
            "expires_at": issued["expires_at"],
            "confirmation_required": True,
            "preview": projection["preview"],
            "validation": _validation_summary(validation),
            "warnings": list(validation.get("warnings") or []),
            "unknowns": [*_profile_unknowns(profile), *projection["unknowns"]],
            "athlete_profile": profile,
        }

    def apply_initialization(
        self, owner_id: str, token: str, body: dict[str, Any]
    ) -> dict[str, Any]:
        """Commit the exact first plan that was previewed, through the one store writer.

        The candidate plan is re-derived here rather than accepted from the request: the
        proposal states what it hashed to, so a request edited after the preview projects
        to something else and stops at the binding check below.
        """
        state_dir = self._state_dir(owner_id)
        request = _object_field(body, "initialization_request")
        opened = self._open_proposal(
            _string_field(body, "proposal"), owner_id=owner_id, kind="initialization"
        )
        if body.get("confirmed") is not True:
            raise GatewayError(HTTPStatus.CONFLICT, "confirmation_required")
        timezone_name, language = self._settings(owner_id)
        # The language has to be the one the preview was rendered in, or the plan
        # re-derived here is a different plan and the proposal stops it. An athlete who
        # changed language mid-confirmation re-previews, which is what they want anyway.
        plan = project_initialization_request(
            request, issued_at=self._issued_at(opened["claims"]), language=language
        )["plan"]
        if opened["claims"].get("plan_hash") != canonical_hash(plan):
            raise GatewayError(HTTPStatus.CONFLICT, "proposal_mismatch")

        if (state_dir / "store.json").is_file():
            current = read_current_plan(state_dir)
            unchanged_first_version = current["current_version"] == 1 and canonical_hash(
                current["current_plan"]
            ) == canonical_hash(plan)
            if unchanged_first_version:
                # The same initialization arriving twice -- a dropped response, a redial
                # -- reads as the success it already was. A store that has moved past its
                # first version is a different situation and fails closed below, because
                # replaying an initialization onto a plan the athlete has since changed
                # would answer about a plan that is no longer theirs.
                return {
                    "status": "passed",
                    **self._envelope(),
                    "plan_id": current["plan_id"],
                    "plan_version": current["current_version"],
                    "idempotent_replay": True,
                    "validation": {"status": "passed", "errors": [], "warnings": []},
                }
            raise self._plan_state_exists(current)

        if opened["expired"]:
            # Nothing exists yet, so this would be a first write against a preview the
            # athlete saw long enough ago that it is no longer the answer to their week.
            raise GatewayError(HTTPStatus.CONFLICT, "proposal_expired")
        validation = self._validate_initial_plan(plan)
        result = init_store(state_dir, plan)
        response = {
            "status": "passed",
            **self._envelope(),
            "plan_id": result["plan_id"],
            "plan_version": result["current_version"],
            "idempotent_replay": False,
            "validation": _validation_summary(validation),
        }
        warnings = self._store_initial_availability(
            state_dir, request, timezone_name=timezone_name
        )
        if warnings:
            response["warnings"] = warnings
        return response

    def _store_initial_availability(
        self, state_dir: Path, request: dict[str, Any], *, timezone_name: str
    ) -> list[str]:
        """Keep the days the athlete named while setting up their first plan (issue #28).

        ``initialization_request.availability`` was previously echoed into the preview and
        then discarded -- correct while nothing could hold it, and a lost fact now that
        something can. The athlete stated it once; the next conversation should not open
        by asking again.

        Runs after the plan is committed and never unwinds it. The plan is the thing the
        athlete confirmed, availability is a note beside it, and rolling back a
        successful initialization over a failed note would be the worse trade by a wide
        margin. A failure is reported as a warning instead, naming what was not kept, so
        the coach can simply ask once more.
        """
        availability = request.get("availability")
        days = availability.get("days") if isinstance(availability, dict) else None
        if not isinstance(days, list) or not days:
            return []
        try:
            athlete_evidence.record_availability(
                state_dir,
                recurring={"available_days": days},
                timezone_name=timezone_name,
                now=self._now(),
            )
        except (AthleteEvidenceError, StateStoreError, GatewayError, OSError) as exc:
            return [f"available days were not stored and will be asked again: {exc}"]
        return []

    def _require_no_plan_state(self, state_dir: Path) -> None:
        if (state_dir / "store.json").is_file():
            raise self._plan_state_exists(read_current_plan(state_dir))

    @staticmethod
    def _plan_state_exists(current: dict[str, Any]) -> GatewayError:
        """An account with a plan is never re-initialised: that would discard its history."""
        return GatewayError(
            HTTPStatus.CONFLICT,
            "plan_state_exists",
            extra={
                "current_plan_id": current["plan_id"],
                "current_plan_version": current["current_version"],
            },
        )

    @staticmethod
    def _validate_initial_plan(plan: dict[str, Any]) -> dict[str, Any]:
        """The same validators every other path uses, applied to a plan with no history.

        ``validate_plan_state`` owns the structure. ``validate_adopted_plan`` asks the
        athlete-fitness half a plan change already gets from ``validate_bundle`` -- which
        needs a before plan, an event and a context a first plan does not have -- so the
        first week is not the one week where an unmeasured pace, heart rate or load could
        reach the athlete, or where an unexecutable session could enter the store.
        """
        structure = validate_plan_state(plan)
        adopted = validate_adopted_plan(plan)
        errors = list(structure.get("errors") or []) + list(adopted.get("errors") or [])
        warnings = list(structure.get("warnings") or []) + list(adopted.get("warnings") or [])
        if plan.get("version") != 1:
            errors.append("initial PlanState version must be 1")
        if errors:
            raise GatewayError(
                HTTPStatus.UNPROCESSABLE_ENTITY,
                "validation_failed",
                extra={
                    "validation": {
                        "status": "blocked",
                        "errors": errors,
                        "warnings": warnings,
                    }
                },
            )
        return {"status": "passed", "errors": [], "warnings": warnings}

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
        """Project one coaching change request and preview it. Writes nothing, ever.

        The request carries coaching judgment only; ``project_change_request`` builds the
        candidate PlanState and DecisionEvent from the store's own current plan, and
        ``validate_bundle`` stays the single authority on whether they may be adopted.
        """
        state_dir = self._state_dir(owner_id)
        context = _object_field(body, "context")
        change_request = _object_field(body, "change_request")
        plan_id = _string_field(body, "plan_id")
        plan_version = _integer_field(body, "plan_version")

        current = read_current_plan(state_dir)
        before = current["current_plan"]
        self._require_current(current, plan_id, plan_version)

        issued_at = self._instant()
        projection = project_change_request(
            before,
            change_request,
            context=context,
            issued_at=issued_at,
            language=self._settings(owner_id)[1],
        )
        after = projection["after_plan"]
        event = projection["decision_event"]
        validation = validate_bundle(context, before, after, event)
        if validation["status"] != "passed":
            raise GatewayError(
                HTTPStatus.UNPROCESSABLE_ENTITY,
                "validation_failed",
                extra={"validation": _validation_summary(validation)},
            )
        issued = self._issue_proposal(
            _decision_claims(
                owner=self._owner_binding(owner_id),
                context=context,
                plan_id=current["plan_id"],
                base_version=current["current_version"],
                after_plan=after,
                decision_event=event,
                # A request that moves nothing has nothing to confirm; asking anyway
                # trains the athlete to confirm without reading.
                confirmation_required=projection["material_change"],
            ),
            now=issued_at,
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
            "proposal": issued["proposal"],
            "expires_at": issued["expires_at"],
            "confirmation_required": projection["material_change"],
            "preview": projection["preview"],
            "validation": _validation_summary(validation),
            "warnings": list(validation.get("warnings") or []),
            "unknowns": unknowns,
        }

    def apply_decision_request(
        self, owner_id: str, token: str, body: dict[str, Any]
    ) -> dict[str, Any]:
        """Commit the exact change that was previewed, or commit nothing.

        The candidate artifacts are re-derived here rather than accepted from the request:
        the proposal states what they hashed to, so a change request edited after the
        preview projects to something else and stops at the binding check below.
        """
        state_dir = self._state_dir(owner_id)
        context = _object_field(body, "context")
        change_request = _object_field(body, "change_request")
        plan_id = _string_field(body, "plan_id")
        plan_version = _integer_field(body, "plan_version")
        opened = self._open_proposal(
            _string_field(body, "proposal"), owner_id=owner_id, kind="decision"
        )
        claims = opened["claims"]

        context_id = context.get("context_id")
        if (
            claims.get("plan_id") != plan_id
            or claims.get("base_version") != plan_version
            or claims.get("context_id") != (context_id if isinstance(context_id, str) else None)
            or claims.get("context_hash") != canonical_hash(context)
        ):
            raise GatewayError(HTTPStatus.CONFLICT, "proposal_mismatch")
        if claims.get("confirmation_required") and body.get("confirmed") is not True:
            raise GatewayError(HTTPStatus.CONFLICT, "confirmation_required")

        current = read_current_plan(state_dir)
        receipt = current["receipt"]
        if (
            receipt.get("plan_hash") == claims.get("after_hash")
            and receipt.get("event_hash") == claims.get("event_hash")
            and receipt.get("context_hash") == claims.get("context_hash")
        ):
            # This exact decision is already the head. A retried request -- a dropped
            # response, a client redial -- must read as the success it actually was, not
            # as a stale-version conflict, and it writes nothing whether or not the
            # proposal's own lifetime has since run out.
            return {
                "status": "passed",
                **self._envelope(),
                "plan_id": current["plan_id"],
                "plan_version": current["current_version"],
                "event_id": receipt.get("event_id"),
                "idempotent_replay": True,
                "validation": {"status": "passed", "errors": [], "warnings": []},
            }
        if opened["expired"]:
            raise GatewayError(HTTPStatus.CONFLICT, "proposal_expired")

        self._require_current(current, plan_id, plan_version)
        before = current["current_plan"]
        projection = project_change_request(
            before,
            change_request,
            context=context,
            issued_at=self._issued_at(claims),
            # The language the preview was written in. A change made since then
            # re-renders the touched sessions differently, and the proposal refuses
            # the mismatch rather than committing prose nobody confirmed.
            language=self._settings(owner_id)[1],
        )
        after = projection["after_plan"]
        event = projection["decision_event"]
        if (
            canonical_hash(after) != claims.get("after_hash")
            or canonical_hash(event) != claims.get("event_hash")
        ):
            raise GatewayError(HTTPStatus.CONFLICT, "proposal_mismatch")

        validation = validate_bundle(context, before, after, event)
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
            "validation": _validation_summary(result.get("validation")),
        }

    def _run_threshold_hr(self, token: str) -> int | None:
        """The account's Run threshold HR, or ``None`` when it cannot be read.

        The hosted token carries ``SETTINGS:READ`` (issue #41), so this is a read the
        hosted entry can make. ``None`` blocks only a workout carrying a heart-rate
        ceiling, and blocks it at preview with one actionable message rather than after
        a provider write -- coaching capability stays entry-agnostic (AGENTS.md 10).
        """
        transport = IntervalsTransport(self._credentials(token), fetch=self.fetch)
        observed, value = transport.run_threshold_hr()
        if not observed or isinstance(value, bool) or not isinstance(value, int) or value < 1:
            return None
        return value

    def prepare_delivery(self, owner_id: str, token: str, body: dict[str, Any]) -> dict[str, Any]:
        """Derive the exact preview set for sessions of the *current* plan. No writes."""
        state_dir = self._state_dir(owner_id)
        plan_id = _string_field(body, "plan_id")
        plan_version = _integer_field(body, "plan_version")
        session_ids = _string_list_field(body, "session_ids")

        current = read_current_plan(state_dir)
        self._require_current(current, plan_id, plan_version)
        proposal_set = prepare_delivery_set(
            current["current_plan"],
            session_ids,
            read_run_threshold_hr=lambda: self._run_threshold_hr(token),
        )
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
                    # The delivered text says `50-86% LTHR` where the plan says 140 bpm.
                    # Without this the athlete would be asked to confirm a percentage
                    # whose meaning only the provider knows.
                    "hr_ceiling_resolution": item["preview"]["hr_ceiling_resolution"],
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
        # One boundary, shared with the CLI: it reserves the store before the first
        # Intervals write and records whatever Intervals accepted, so a set that fails
        # halfway still reports the events that exist (issue #110).
        receipt = deliver_approved_set(
            state_dir, delivery_set, approval, transport=transport
        )
        state_update = receipt["state_update"]
        return {
            "status": receipt["status"],
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
            # Present and empty on a clean delivery; on a partial one it names every
            # session the athlete asked for that Intervals does not hold.
            "unresolved": receipt["unresolved"],
            # True when this store is still holding a reservation because Intervals may
            # hold an effect nothing has reconciled. Retrying this same delivery_set is
            # what converges it; no plan change is possible until it does (issue #121).
            "attempt_open": receipt["attempt_open"],
            "state_update": {
                "event_id": state_update.get("event_id"),
                "idempotent_replay": state_update["idempotent_replay"],
                "session_ids": state_update["session_ids"],
                "external_ids": state_update["external_ids"],
            },
        }

    def prepare_withdrawal(self, owner_id: str, token: str, body: dict[str, Any]) -> dict[str, Any]:
        """Preview which superseded Intervals events would be removed. Writes nothing."""
        state_dir = self._state_dir(owner_id)
        plan_id = _string_field(body, "plan_id")
        plan_version = _integer_field(body, "plan_version")
        session_ids = _string_list_field(body, "session_ids")

        current = read_current_plan(state_dir)
        self._require_current(current, plan_id, plan_version)
        proposal_set = prepare_withdrawal_set(current["current_plan"], session_ids)
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
                    "scheduled_date": item["scheduled_date"],
                    "superseded_external_id": item["superseded_external_id"],
                }
                for item in proposal_set["items"]
            ],
            "withdrawal_set": proposal_set,
        }

    def apply_withdrawal(self, owner_id: str, token: str, body: dict[str, Any]) -> dict[str, Any]:
        """Remove the confirmed superseded events, and record only what was verified gone."""
        state_dir = self._state_dir(owner_id)
        withdrawal_set = _object_field(body, "withdrawal_set")
        proposal_hash = _string_field(body, "proposal_hash")
        if body.get("confirmed") is not True:
            raise GatewayError(HTTPStatus.CONFLICT, "confirmation_required")
        if withdrawal_set.get("proposal_hash") != proposal_hash:
            raise GatewayError(HTTPStatus.CONFLICT, "proposal_hash_mismatch")

        approval = approve_withdrawal_set(withdrawal_set, approved_by=f"owner:{owner_id}")
        transport = IntervalsTransport(self._credentials(token), fetch=self.fetch)
        receipt = withdraw_approved_set(
            state_dir,
            withdrawal_set,
            approval,
            transport=transport,
            today=self._local_day(owner_id, body),
        )
        state_update = receipt["state_update"]
        return {
            "status": receipt["status"],
            **self._envelope(),
            "plan_id": state_update["plan_id"],
            "plan_version": state_update["current_version"],
            "receipt_id": receipt["receipt_id"],
            "proposal_hash": receipt["proposal_hash"],
            "withdrawn": [
                {
                    "session_id": item["session_id"],
                    "external_id": item["withdrawn_external_id"],
                }
                for item in receipt["withdrawn"]
            ],
            "unresolved": receipt["unresolved"],
            "attempt_open": receipt["attempt_open"],
        }

    def clear_delivery_attempt(
        self, owner_id: str, token: str, body: dict[str, Any]
    ) -> dict[str, Any]:
        """Abandon a delivery reservation the athlete can no longer retry. Consequential.

        Retrying the same confirmed set is always the better answer -- it converges
        without a second event -- but the confirmed set lives only in the conversation
        that prepared it, and a conversation that ended took it with it. That left the
        documented recovery on the server's local CLI, which a hosted athlete has no way
        to reach.

        So this exists, and it is deliberately the smaller of the two paths: it releases
        the fence and reconciles nothing. Whatever the journal still names is now the
        athlete's to check on the Intervals calendar, which is why it is bound to the
        exact reservation they were shown -- an id that no longer names what the store
        holds clears nothing -- and why the response repeats what it just abandoned.

        The owner comes from the bearer token alone. There is no body field that could
        name a different athlete's store.
        """
        state_dir = self._state_dir(owner_id)
        attempt_id = _string_field(body, "attempt_id")
        if body.get("confirmed") is not True:
            raise GatewayError(HTTPStatus.CONFLICT, "confirmation_required")

        open_attempt = pending_delivery_attempt(state_dir)
        if open_attempt is None:
            # Idempotent on purpose: a repeated clear, or one for a delivery that
            # converged on its own, is a no-op to report -- not a failure, and not a
            # reason to touch a store that is no longer fenced.
            return {
                "status": "passed",
                **self._envelope(),
                "cleared": False,
                "attempt_id": None,
                "abandoned": [],
                "detail": "this account holds no delivery reservation; nothing was cleared",
            }
        if open_attempt["attempt_id"] != attempt_id:
            # The athlete confirmed one reservation and the store holds another, so the
            # confirmation covers nothing here. Report the one that is actually open.
            raise GatewayError(
                HTTPStatus.CONFLICT,
                "attempt_mismatch",
                extra={"unresolved_delivery": _attempt_view(open_attempt)},
            )

        # The same store function the local CLI recovery uses, so there is one definition
        # of what releasing a reservation means; the id is re-checked under its lock.
        report = close_delivery_attempt(
            state_dir, attempt_id=attempt_id, abandon_unresolved=True
        )
        abandoned = [
            {
                "session_id": operation["session_id"],
                "operation": operation["operation"],
                "state": operation["state"],
                "external_id": operation["external_id"],
            }
            for operation in report["abandoned"]
        ]
        return {
            "status": "passed",
            **self._envelope(),
            "cleared": report["cleared"],
            "attempt_id": attempt_id,
            "abandoned": abandoned,
            "detail": (
                "the delivery reservation is released and plan changes are possible again; "
                + (
                    "this product no longer tracks the operations listed above, so the "
                    "Intervals calendar is now the only record of whether they landed"
                    if abandoned
                    else "nothing had reached Intervals under it"
                )
            ),
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


# The Custom GPT entry's settled contract: a redirect to Intervals, and a token endpoint
# that injects the client secret. Unchanged, and deliberately separate from the
# authorization server below -- a configured Action cannot re-run a discovery document.
AUTHORIZE_PATH = "/oauth/intervals/authorize"
TOKEN_PATH = "/oauth/intervals/token"
# This gateway's own authorization server. `AUTHORIZATION_PATH` is where a client starts,
# `CALLBACK_PATH` is what Intervals is told to come back to, and `ACCESS_TOKEN_PATH`
# issues the token the MCP entry accepts.
AUTHORIZATION_PATH = "/oauth/authorize"
CALLBACK_PATH = "/oauth/callback"
ACCESS_TOKEN_PATH = "/oauth/token"
# RFC 7591 dynamic client registration. An MCP client that has never been configured with
# a client id asks here for one; see `CoachGateway.register_client` for why it is answered
# without a secret.
REGISTRATION_PATH = "/oauth/register"
MCP_PATH = "/mcp"
# RFC 9728 and RFC 8414. Both are anonymous by construction: a client reads them precisely
# because it does not yet hold a token.
PROTECTED_RESOURCE_METADATA_PATH = "/.well-known/oauth-protected-resource"
AUTHORIZATION_SERVER_METADATA_PATH = "/.well-known/oauth-authorization-server"
# Both RFCs also define a path-aware form, where the protected resource's own path is
# appended to the well-known prefix. The resource here is `/mcp`, so a conforming client
# may look under either spelling; both are served, with the same document, because a
# client that finds neither cannot start an authorization at all.
_METADATA_PATH_SUFFIX = MCP_PATH

# path -> (allowed method, route kind). Unknown paths 404, known paths with the wrong
# method 405; neither reaches an owner or a provider.
ROUTES: dict[str, tuple[str, str]] = {
    "/healthz": ("GET", "health"),
    "/readyz": ("GET", "readiness"),
    AUTHORIZE_PATH: ("GET", "authorize"),
    TOKEN_PATH: ("POST", "token"),
    AUTHORIZATION_PATH: ("GET", "gateway_authorize"),
    CALLBACK_PATH: ("GET", "gateway_callback"),
    ACCESS_TOKEN_PATH: ("POST", "gateway_token"),
    REGISTRATION_PATH: ("POST", "client_registration"),
    PROTECTED_RESOURCE_METADATA_PATH: ("GET", "protected_resource_metadata"),
    PROTECTED_RESOURCE_METADATA_PATH + _METADATA_PATH_SUFFIX: (
        "GET",
        "protected_resource_metadata",
    ),
    AUTHORIZATION_SERVER_METADATA_PATH: ("GET", "authorization_server_metadata"),
    AUTHORIZATION_SERVER_METADATA_PATH + _METADATA_PATH_SUFFIX: (
        "GET",
        "authorization_server_metadata",
    ),
    # POST only. A GET here would be the request to open an SSE stream, which this
    # stateless server does not serve, so it is refused as a wrong method rather than
    # answered with an empty stream the client would then wait on.
    MCP_PATH: ("POST", "mcp"),
    "/v1/coach/session": ("POST", "session"),
    "/v1/coach/permissions": ("GET", "permissions"),
    "/v1/coach/profile": ("POST", "profile_record"),
    "/v1/coach/availability": ("POST", "availability_record"),
    "/v1/coach/strength-report": ("POST", "strength_report"),
    "/v1/coach/strength-prescribed": ("POST", "strength_prescribed_confirm"),
    "/v1/coach/initialization/prepare": ("POST", "initialization_prepare"),
    "/v1/coach/initialization/apply": ("POST", "initialization_apply"),
    "/v1/coach/decision/prepare": ("POST", "decision_prepare"),
    "/v1/coach/decision/apply": ("POST", "decision_apply"),
    "/v1/coach/delivery/prepare": ("POST", "delivery_prepare"),
    "/v1/coach/delivery/publish": ("POST", "delivery_publish"),
    "/v1/coach/delivery/withdraw/prepare": ("POST", "withdrawal_prepare"),
    "/v1/coach/delivery/withdraw/apply": ("POST", "withdrawal_apply"),
    "/v1/coach/delivery/attempt/clear": ("POST", "delivery_attempt_clear"),
}


# A host, with an optional port or a bracketed IPv6 literal. Anything else is refused
# rather than trimmed: this value is interpolated into a discovery document and into a
# `WWW-Authenticate` header, and a header value is exactly where a quote or a newline
# from an attacker-supplied Host would do damage.
_PUBLIC_HOST = re.compile(r"^(?:[A-Za-z0-9._~-]+|\[[0-9A-Fa-f:.]+\])(?::\d{1,5})?$")


def public_base_url(headers: Any) -> str | None:
    """The origin the client actually reached this server on, or ``None`` if unusable.

    OAuth discovery documents state absolute URLs, so the server has to know its own
    public origin -- which, behind a platform's TLS terminator, is not the address it
    bound. ``X-Forwarded-Proto``/``X-Forwarded-Host`` are what that hop leaves behind and
    are therefore preferred; ``Host`` answers for a direct connection. A proxy chain
    leaves a comma-separated list, of which only the first entry is the client's own.

    This process never terminates TLS, so an unforwarded request is plain ``http`` by
    construction rather than by assumption. Nothing here is configuration either: a
    domain the operator would have to keep in step with the deployed one is a second
    source of truth for the same fact.
    """
    forwarded_host = str(headers.get("X-Forwarded-Host") or "").split(",")[0].strip()
    host = forwarded_host or str(headers.get("Host") or "").strip()
    if not _PUBLIC_HOST.fullmatch(host):
        return None
    scheme = str(headers.get("X-Forwarded-Proto") or "").split(",")[0].strip().lower()
    return f"{scheme if scheme in {'http', 'https'} else 'http'}://{host}"


def _origin(raw: Any) -> str | None:
    """One web origin reduced to the triple that defines it, or ``None`` if it is not one.

    RFC 6454: an origin is a scheme, a host and a port, and nothing else -- so a value
    carrying a path, a query, a fragment or userinfo is not an origin and is not read as
    a nearly-correct one. Scheme and host are lower-cased because they are
    case-insensitive; the port is compared as written, since it is digits either way.

    Reducing both sides to this before comparing is what keeps the comparison exact.
    ``https://claude.ai.evil.example`` and ``https://claude.ai:8443`` are each a different
    origin from ``https://claude.ai``, which a prefix or suffix test would not have said.
    """
    value = str(raw or "").strip()
    if not value:
        return None
    try:
        parts = urllib.parse.urlsplit(value)
    except ValueError:
        return None
    if parts.scheme.lower() not in {"http", "https"}:
        return None
    if parts.path or parts.query or parts.fragment or parts.username or parts.password:
        return None
    host = parts.netloc.lower()
    if not _PUBLIC_HOST.fullmatch(host):
        return None
    return f"{parts.scheme.lower()}://{host}"


# The Intervals scopes this product asks for -- the same four the Custom GPT entry
# declares in its OpenAPI security block, which the MCP contract tests hold this tuple
# to. Advertising them here lets an MCP client put a concrete `scope` on the authorize
# request instead of leaving Intervals to grant whatever its default is.
INTERVALS_OAUTH_SCOPES: tuple[str, ...] = (
    "ACTIVITY:READ",
    "WELLNESS:READ",
    "CALENDAR:WRITE",
    "SETTINGS:READ",
)


def protected_resource_metadata(base_url: str) -> dict[str, Any]:
    """RFC 9728: which authorization server guards this MCP endpoint.

    One document, naming this same gateway as its own authorization server -- which it
    now is in full: it holds the authorization state, verifies the PKCE challenge, and
    issues the token ``/mcp`` accepts. Intervals is where the athlete consents, not what
    the client authenticates to.
    """
    return {
        "resource": f"{base_url}{MCP_PATH}",
        "authorization_servers": [base_url],
        "bearer_methods_supported": ["header"],
    }


def authorization_server_metadata(base_url: str) -> dict[str, Any]:
    """RFC 8414 for this gateway's own authorization server.

    The endpoints named here are this gateway's: ``/oauth/authorize`` starts a flow it
    runs on the athlete's behalf against Intervals, and ``/oauth/token`` issues a token
    of this gateway's own. ``token_endpoint_auth_method: none`` follows from the client
    being public -- it has no secret to present, and the Intervals secret stays in this
    process.

    ``code_challenge_methods_supported`` is now a statement of fact rather than of
    intent: the challenge is held in this gateway's own authorization state and the
    verifier is checked at the token endpoint (see ``CoachGateway.issue_access_token``).
    Intervals still implements no PKCE, and no longer needs to -- the leg it runs is a
    server-to-server exchange with a secret the client never sees.
    """
    return {
        "issuer": base_url,
        "authorization_endpoint": f"{base_url}{AUTHORIZATION_PATH}",
        "token_endpoint": f"{base_url}{ACCESS_TOKEN_PATH}",
        "registration_endpoint": f"{base_url}{REGISTRATION_PATH}",
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code"],
        "code_challenge_methods_supported": ["S256"],
        "token_endpoint_auth_methods_supported": ["none"],
        "scopes_supported": list(INTERVALS_OAUTH_SCOPES),
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
        credential into a log file. ``_dispatch`` logs only method, path, status and the
        broad authenticated/anonymous class -- never a stable owner identifier. This
        design also never puts a token in a URL, which keeps that line safe to write.
        """

    def _dispatch(self, method: str) -> None:
        owner_id: str | None = None
        redirect_location: str | None = None
        path = urllib.parse.urlsplit(self.path).path
        try:
            route = ROUTES.get(path)
            if route is None:
                raise GatewayError(HTTPStatus.NOT_FOUND, "not_found")
            allowed_method, kind = route
            if method != allowed_method:
                raise GatewayError(HTTPStatus.METHOD_NOT_ALLOWED, "method_not_allowed")
            gateway: CoachGateway = self.server.gateway  # type: ignore[attr-defined]
            if kind in {"health", "readiness"}:
                payload = gateway.health()
                status = (
                    HTTPStatus.OK
                    if kind == "health" or payload["status"] == "ok"
                    else HTTPStatus.SERVICE_UNAVAILABLE
                )
            elif kind == "authorize":
                # ChatGPT can be configured with this Gateway URL as its authorization
                # endpoint. Keep the OAuth provider as the source of truth and forward the
                # signed query; no token, state, or owner is read here.
                #
                # The one thing not forwarded verbatim is `scope`, which is narrowed to
                # what this product declares exactly as `/oauth/authorize` narrows it. A
                # configured Action asking for a wider grant would put it in front of the
                # athlete on the Intervals consent screen, and which entry the request
                # arrived through is not a reason for that to be allowed here and refused
                # there.
                redirect_location = INTERVALS_AUTHORIZE_URL + _narrowed_scope_query(
                    urllib.parse.urlsplit(self.path).query
                )
                status = HTTPStatus.TEMPORARY_REDIRECT
            elif kind == "token":
                payload = gateway.exchange_token(self._form_body())
                status = HTTPStatus.OK
            elif kind == "gateway_authorize":
                redirect_location = gateway.start_authorization(
                    self._query(), base_url=self._require_public_base_url()
                )
                status = HTTPStatus.FOUND
            elif kind == "gateway_callback":
                # Where the athlete lands after Intervals. Everything this hop needs
                # travels in the state it is carrying, so no owner is resolved here and
                # no request body is read.
                redirect_location = gateway.complete_authorization(self._query())
                status = HTTPStatus.FOUND
            elif kind == "gateway_token":
                payload = gateway.issue_access_token(
                    self._form_body(), base_url=self._require_public_base_url()
                )
                status = HTTPStatus.OK
            elif kind == "client_registration":
                payload = gateway.register_client(self._json_body())
                status = HTTPStatus.CREATED
            elif kind in {"protected_resource_metadata", "authorization_server_metadata"}:
                base_url = self._require_public_base_url()
                payload = (
                    protected_resource_metadata(base_url)
                    if kind == "protected_resource_metadata"
                    else authorization_server_metadata(base_url)
                )
                status = HTTPStatus.OK
            elif kind == "mcp":
                # Transport before identity: which browser is calling, and which revision
                # of the protocol it is speaking, are answerable from the headers alone,
                # so neither costs a token lookup.
                self._require_allowed_origin(gateway)
                self._require_supported_protocol_version()
                # Identity first here too, and for the same reason: the JSON-RPC message
                # is not parsed, and no tool name is even read, until the token names an
                # owner. The bearer is this gateway's own token, and the provider
                # credential comes back out of it for the routes that need one.
                owner_id, provider_token = gateway.resolve_mcp_owner(
                    _bearer_token(self.headers.get("Authorization")),
                    base_url=public_base_url(self.headers),
                )
                status, payload = mcp_transport.handle(
                    self._read_body("application/json"),
                    call_tool=self._mcp_tool_call(gateway, owner_id, provider_token),
                    server_version=API_VERSION,
                )
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
        if redirect_location is not None:
            self._send_redirect(int(status), redirect_location)
        else:
            self._send_json(int(status), payload, headers=self._challenge(path, int(status)))
        LOGGER.info(
            "%s %s -> %s access=%s",
            method,
            path,
            int(status),
            "authenticated" if owner_id is not None else "anonymous",
        )

    def _query(self) -> dict[str, str]:
        """This request's query parameters, first value only.

        A repeated parameter is an ambiguous request, and OAuth reads the first one; the
        alternative -- taking the last -- is how a duplicated ``redirect_uri`` overrides
        the one that was checked.
        """
        parsed = urllib.parse.parse_qs(
            urllib.parse.urlsplit(self.path).query, keep_blank_values=True
        )
        return {key: values[0] for key, values in parsed.items() if values}

    def _require_allowed_origin(self, gateway: CoachGateway) -> None:
        """Refuse a browser calling ``/mcp`` from an origin this server does not answer to.

        MCP requires this because of DNS rebinding: a page on any domain can be made to
        resolve to a loopback address and then talk to whatever is listening there, and
        the athlete's own gateway is exactly what would be listening. The ``Origin``
        header is the browser's unforgeable statement of which page that is.

        An absent header is allowed, and has to be: a server-side MCP client -- which is
        every non-browser client, including the ones this product is actually reached
        from -- sends none, and refusing them would be refusing the normal case to defend
        against the rare one. A header that is *present* is a browser, and only two
        answers are trusted: this request's own origin, and the connector hosts named in
        ``MCP_ALLOWED_ORIGINS`` or by the operator. Anything else, including a value that
        is not a well-formed origin at all, is a ``403``.
        """
        raw = self.headers.get("Origin")
        if raw is None:
            return
        allowed = {*MCP_ALLOWED_ORIGINS, *gateway.config.allowed_mcp_origins}
        own = _origin(public_base_url(self.headers))
        if own is not None:
            allowed.add(own)
        if _origin(raw) not in allowed:
            raise GatewayError(HTTPStatus.FORBIDDEN, "forbidden_origin")

    def _require_supported_protocol_version(self) -> None:
        """Refuse a header naming a protocol revision this server does not implement.

        Separate from the ``initialize`` negotiation in ``mcp_transport``, and both are
        required: the handshake settles which revision the connection uses, the header
        states it on every subsequent request so a stateless server does not have to
        remember. An absent header is accepted -- 2025-06-18 says it means 2025-03-26 --
        and a value outside ``HTTP_PROTOCOL_VERSIONS`` is a ``400``, because a client
        speaking a revision this server does not implement is better told so than served
        a response it may read wrongly.
        """
        raw = self.headers.get("MCP-Protocol-Version")
        if raw is None:
            return
        if raw.strip() not in mcp_transport.HTTP_PROTOCOL_VERSIONS:
            raise GatewayError(HTTPStatus.BAD_REQUEST, "unsupported_protocol_version")

    def _require_public_base_url(self) -> str:
        """This request's own origin, or a refusal -- never a guessed domain.

        A discovery document that named the wrong origin would send the client to
        authorize somewhere else, so a request whose Host is missing or unusable is
        answered as the malformed request it is.
        """
        base_url = public_base_url(self.headers)
        if base_url is None:
            raise _invalid("Host header is missing or unusable")
        return base_url

    def _mcp_tool_call(
        self, gateway: CoachGateway, owner_id: str, token: str
    ) -> Callable[[str, dict[str, Any]], dict[str, Any]]:
        """Bind one authenticated caller to the same dispatch the REST paths use.

        This is the whole join between the two entries: MCP adds a transport, not a
        second route table, so a tool can never reach a handler `/v1/coach/*` cannot.
        """

        def call(kind: str, arguments: dict[str, Any]) -> dict[str, Any]:
            try:
                return gateway.route(kind, owner_id, token, arguments)
            except GatewayError as exc:
                if exc.upstream_unauthorized:
                    # The provider refused this athlete's credential, so there is nothing
                    # the model can do with a tool result: the client has to authorize
                    # again, and it starts that on a transport-level 401 with the
                    # challenge below. This is what makes a revoked or expired connection
                    # heal itself instead of reading as a broken server. The REST entry
                    # keeps reporting the same failure as `provider_error`.
                    raise GatewayError(HTTPStatus.UNAUTHORIZED, "unauthorized") from exc
                raise mcp_transport.ToolCallBlocked(exc.payload()) from exc

        return call

    def _challenge(self, path: str, status: int) -> dict[str, str]:
        """Point an unauthenticated MCP client at the metadata that starts OAuth.

        RFC 9728's challenge, and only on ``/mcp``: an MCP client discovers where to
        authorize from this header, while the Custom GPT entry's 401 bodies are a settled
        contract that gains nothing from it.
        """
        if path != MCP_PATH or status != HTTPStatus.UNAUTHORIZED:
            return {}
        base_url = public_base_url(self.headers)
        if base_url is None:
            return {}
        return {
            "WWW-Authenticate": (
                f'Bearer resource_metadata="{base_url}{PROTECTED_RESOURCE_METADATA_PATH}"'
            )
        }

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

    def _send_redirect(self, status: int, location: str) -> None:
        self._drain()
        self.send_response(status)
        self.send_header("Location", location)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _send_json(
        self,
        status: int,
        payload: dict[str, Any] | None,
        *,
        headers: dict[str, str] | None = None,
    ) -> None:
        """``payload`` is ``None`` only for an accepted MCP notification, which has no body."""
        self._drain()
        body = b"" if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        if payload is not None:
            self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        if body and self.command != "HEAD":
            self.wfile.write(body)


class CoachGatewayServer(ThreadingHTTPServer):
    # False, unlike ``ThreadingHTTPServer``'s own default: graceful shutdown means a
    # request that is already running gets to finish and send its response, not get cut
    # off the instant the accept loop stops. ``False`` makes ``ThreadingMixIn``'s own
    # ``server_close()`` join every request thread it started before returning (see
    # ``run_gateway``); each of those threads is itself bounded by
    # ``source_intervals.REQUEST_TIMEOUT_SECONDS``, so the join is bounded too, and the
    # host platform's own kill timeout remains the hard backstop for anything that still
    # overruns it.
    daemon_threads = False

    def __init__(self, address: tuple[str, int], handler_class: type, *, gateway: CoachGateway):
        self.gateway = gateway
        super().__init__(address, handler_class)


def _probe_state_root_writable(state_root: Path) -> None:
    """Prove the state root actually accepts writes, not merely that it can be created.

    ``mkdir(exist_ok=True)`` (see ``run_preflight``) already succeeds against a directory
    that exists but refuses writes -- a bind mount still read-only, a volume attached
    before its permissions were fixed. Creating and removing one throwaway file is the
    cheapest real proof, and running it once at startup means a bad mount is a refused
    boot rather than a 500 on whichever athlete's request first tries to write.
    """
    try:
        descriptor, temporary_name = tempfile.mkstemp(prefix=".preflight-", dir=state_root)
    except OSError as exc:
        # `strerror`, not `str(exc)`: the latter interpolates the full path, and this
        # function's only caller is the same startup path that never echoes a configured
        # value back (see `load_config`'s own docstring).
        raise GatewayConfigError(
            f"gateway state root is not writable: {exc.strerror or exc}"
        ) from exc
    try:
        os.close(descriptor)
    finally:
        Path(temporary_name).unlink(missing_ok=True)


def _reap_stale_owner_locks(
    state_root: Path,
    *,
    startup_drain_seconds: float = 0.0,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    """Wait out a hosted predecessor, then remove leftover owner ``.lock`` markers.

    Exactly one configured replica does not mean exactly one live process during a
    rolling deploy: Railway and similar hosts can start the replacement while the old
    process is still draining. When a lock exists at startup, hosted startup therefore
    waits longer than the platform's drain/kill window before rescanning. At that point a
    remaining marker is the remnant of a predecessor that never reached
    ``_exclusive_lock``'s ``finally``; a predecessor that drained cleanly has already
    removed its own marker. When no lock exists there is nothing to reclaim, so startup
    is immediate. Local development passes a zero wait.

    This is still safe only under the deployment contract of one configured replica. A
    permanently live sibling would survive the grace period, and its lock would be
    indistinguishable from a crashed predecessor's marker.

    Logs nothing itself; the caller logs only the count this returns, on the same
    principle this module states at the top: nothing this transport logs ever names an
    owner or a path (a property ``test_logs_and_error_bodies_carry_no_credential_material``
    holds the rest of this module to).
    """
    owners_dir = state_root / "owners"
    if not owners_dir.is_dir():
        return 0
    # Most deploys start while no request holds an owner lock. In that common case there
    # is nothing to reclaim and no reason to keep the replacement unready for the entire
    # drain window. If a predecessor is visibly active, wait it out before deciding that
    # any surviving marker is stale.
    lock_present = any(
        owner_dir.is_dir() and (owner_dir / ".lock").is_file()
        for owner_dir in owners_dir.iterdir()
    )
    if not lock_present:
        return 0
    if startup_drain_seconds > 0:
        sleep(startup_drain_seconds)

    reclaimed = 0
    for owner_dir in owners_dir.iterdir():
        if not owner_dir.is_dir():
            continue
        try:
            (owner_dir / ".lock").unlink()
        except FileNotFoundError:
            continue
        except OSError:
            # Not this process's to force past; a doctor-store run against this one
            # owner will surface whatever is actually wrong (permissions, a read-only
            # mount) with the detail an unattended startup log must not carry.
            continue
        reclaimed += 1
    return reclaimed


def run_preflight(
    config: GatewayConfig,
    *,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    """Fail startup on a broken deployment rather than on somebody's first request.

    Runs once, before a socket is bound: the state root must exist and actually accept
    writes, and the identity registry must open, or nothing here has to guess why sign-in
    or the first read failed hours later. Returns the number of stale owner locks
    reclaimed, purely so the caller can log a count (see ``_reap_stale_owner_locks``).
    """
    try:
        config.state_root.mkdir(parents=True, mode=0o700, exist_ok=True)
    except OSError as exc:
        raise GatewayConfigError(
            f"gateway state root is not usable: {exc.strerror or exc}"
        ) from exc
    _probe_state_root_writable(config.state_root)
    try:
        ensure_registry(config.identity_db_path)
    except IdentityError as exc:
        # `exc`'s own message already leads with "identity registry is unusable:" (see
        # `ensure_registry`); prefixing it again here would just repeat that phrase.
        raise GatewayConfigError(f"gateway {exc}") from exc
    return _reap_stale_owner_locks(
        config.state_root,
        startup_drain_seconds=config.startup_drain_seconds,
        sleep=sleep,
    )


def run_gateway(config: GatewayConfig, *, gateway: CoachGateway | None = None) -> None:
    """Serve until SIGTERM, SIGINT, or an old-style Ctrl-C interrupt, then drain and exit.

    A hosting platform's redeploy is a routine SIGTERM, not an operator error, so it has
    to leave the store exactly as clean as an ordinary idle moment would: every in-flight
    request gets to finish (see ``CoachGatewayServer.daemon_threads``) before the
    listening socket closes. SIGINT gets the same handling, so a local ``Ctrl-C`` drains
    the same way a deployed replica does.

    ``server.shutdown()`` has to run on a thread other than the one inside
    ``serve_forever()``, or it deadlocks waiting for a loop it can never watch exit from
    the inside -- the signal handler below exists to start exactly that thread and
    nothing else.
    """
    reclaimed = run_preflight(config)
    LOGGER.info("reclaimed %d stale owner lock(s) at startup", reclaimed)
    server = CoachGatewayServer(
        (config.host, config.port),
        CoachGatewayHandler,
        gateway=gateway or CoachGateway(config),
    )
    LOGGER.info("gateway listening on %s:%s", config.host, server.server_address[1])

    def _stop(signum: int, frame: Any) -> None:
        del frame
        LOGGER.info("received signal %d; draining in-flight requests before exit", signum)
        threading.Thread(target=server.shutdown, daemon=True).start()

    previous_handlers = {
        sig: signal.signal(sig, _stop) for sig in (signal.SIGTERM, signal.SIGINT)
    }
    try:
        server.serve_forever()
    except KeyboardInterrupt:  # pragma: no cover - defensive; SIGINT is handled above
        pass
    finally:
        for sig, handler in previous_handlers.items():
            signal.signal(sig, handler)
        server.server_close()
