"""Build a CoachContext activity/recovery domain from the athlete's own intervals.icu
account via its read-only REST API.

This is the product path: the default source, and the only one a fresh clone-and-run
install needs. Any user who connects Garmin -> intervals.icu themselves and pastes one
API key gets the full Coach Loop -- no personal-os infrastructure, no local health.db,
and (per ``resolve_credentials`` below) no requirement to even have this repository
checked out with a populated root ``.env``.

GET requests only -- never POST/PUT/DELETE. Never prints, logs, or embeds the API key;
only the ``Authorization`` header carries it, and errors never include header values.
"""

from __future__ import annotations

import base64
import contextvars
import datetime as dt
import json
import os
import time
import urllib.error
import urllib.request
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterator

from .context_core import (
    BuildWindow,
    ContextBuildError,
    SourceDomain,
    _classify_running,
    coverage_entry,
    _measured_number,
    _median_trend,
    _safe_float,
)
from .fit_sets import FitParseError, summarise_sets
from .store import REPO_ROOT


BASE_URL = "https://intervals.icu/api/v1/athlete/{athlete_id}"
# Per-activity reads hang off the activity, not the athlete, so they cannot go through
# BASE_URL. Verified live 2026-08-14 against the real account.
ACTIVITY_URL = "https://intervals.icu/api/v1/activity/{activity_id}"
# A custom User-Agent is REQUIRED: intervals.icu returns 403 for the default
# python-urllib UA (verified live 2026-08-10 against the real account -- no UA -> 403,
# the same key with a UA -> 200).
USER_AGENT = "garmin-coach-loop/0.1"
REQUEST_TIMEOUT_SECONDS = 15
SOURCE_NAME = "intervals-icu-api"

API_KEY_ENV_VAR = "INTERVALS_ICU_API_KEY"
ATHLETE_ID_ENV_VAR = "INTERVALS_ICU_ATHLETE_ID"

# Per-user config file: works for anyone regardless of whether this repository is even
# checked out. This is the second tier of resolve_credentials's precedence, ahead of the
# repo-root .env compatibility fallback.
USER_CONFIG_ENV_PATH = Path.home() / ".config" / "garmin-coach-loop" / ".env"


@dataclass(frozen=True)
class IntervalsCredentials:
    """One athlete's read credentials, in either of the two schemes Intervals accepts.

    ``auth_scheme`` defaults to ``"basic"``, the personal-API-key path every existing
    caller uses. ``"bearer"`` is the OAuth path: ``api_key`` then carries the OAuth access
    token verbatim and ``athlete_id`` is always ``"0"``, which Intervals resolves to
    whichever athlete the bearer token belongs to. Nothing else differs -- the same GETs,
    the same mapping, the same read-only guarantee.
    """

    api_key: str
    athlete_id: str
    auth_scheme: str = "basic"


@dataclass(frozen=True)
class RecentActivity:
    """What the provider holds about recent training, for a caller that reads only that.

    Deliberately not a ``SourceDomain`` with the recovery half left empty. Those fields
    are coverage entries and trends, and an entry that was never populated is
    indistinguishable, at every reader downstream, from one whose provider answered and
    had nothing to report -- exactly the "unread reads as measured-nothing" confusion
    AGENTS.md 3 exists to prevent. A value that can only describe recent activity cannot
    be mistaken for a statement about recovery.
    """

    actuals_window_start: dt.date
    activity_days: frozenset[dt.date]
    recent_actuals: list[dict[str, Any]]


@dataclass
class ProviderResponse:
    """One provider answer: the body, plus the two quota headers Intervals sends.

    Intervals returns ``X-RateLimit-Limit`` and ``X-RateLimit-Remaining`` on every
    response, and until issue #260 both were read off the socket and thrown away --
    this product could not state its own quota, its consumption, or its headroom.
    The fetch seam therefore hands back the pair alongside the body. Both are None
    when the response did not carry them (a test double, a proxy that strips them);
    None means unobserved, never zero (AGENTS.md 3).
    """

    body: bytes
    rate_limit: int | None = None
    rate_remaining: int | None = None


# A callable that performs one GET given a fully-prepared Request and returns the
# response. The default implementation is real urllib; tests inject a fake so the
# unit suite never touches the network.
Fetcher = Callable[[urllib.request.Request], "ProviderResponse"]


def _rate_limit_value(headers: Any, name: str) -> int | None:
    """One quota header as an int, or None when absent or unreadable."""
    raw = headers.get(name) if headers is not None else None
    if raw is None:
        return None
    try:
        value = int(str(raw).strip())
    except ValueError:
        return None
    return value if value >= 0 else None


@dataclass
class ProviderQuotaScope:
    """What one gateway request spent against the shared Intervals pool.

    The quota is granted per registered application, not per athlete, so these
    figures carry no athlete identity by construction -- which is what lets the
    access log print them under the transport's own rule that nothing logged names
    an owner, a path, or a credential. ``calls`` counts attempts, including ones
    the provider refused: a refused request spends the pool too.
    """

    calls: int = 0
    rate_limit: int | None = None
    rate_remaining: int | None = None
    tool: str | None = None


_QUOTA_SCOPE: contextvars.ContextVar[ProviderQuotaScope | None] = contextvars.ContextVar(
    "garmin_coach_loop_provider_quota_scope", default=None
)


@contextmanager
def provider_quota_scope() -> Iterator[ProviderQuotaScope]:
    """Collect provider spend for one request, on whatever thread serves it.

    A context variable rather than gateway state because the gateway is a
    ``ThreadingHTTPServer``: two in-flight requests must not read each other's
    count. Both transports record into the ambient scope when one is open and do
    nothing when none is -- the CLI and the tests pay nothing for it.
    """
    scope = ProviderQuotaScope()
    token = _QUOTA_SCOPE.set(scope)
    try:
        yield scope
    finally:
        _QUOTA_SCOPE.reset(token)


def current_provider_quota() -> ProviderQuotaScope | None:
    """The open scope, for whoever writes the log line; None outside any request."""
    return _QUOTA_SCOPE.get()


def count_provider_call() -> None:
    scope = _QUOTA_SCOPE.get()
    if scope is not None:
        scope.calls += 1


def note_provider_quota(headers: Any) -> None:
    """Record the latest quota reading; absent headers leave the last one standing."""
    scope = _QUOTA_SCOPE.get()
    if scope is None:
        return
    remaining = _rate_limit_value(headers, "X-RateLimit-Remaining")
    limit = _rate_limit_value(headers, "X-RateLimit-Limit")
    if remaining is not None:
        scope.rate_remaining = remaining
    if limit is not None:
        scope.rate_limit = limit


def note_provider_quota_values(rate_limit: int | None, rate_remaining: int | None) -> None:
    """The same record, from a ``ProviderResponse`` already parsed by the fetcher."""
    scope = _QUOTA_SCOPE.get()
    if scope is None:
        return
    if rate_remaining is not None:
        scope.rate_remaining = rate_remaining
    if rate_limit is not None:
        scope.rate_limit = rate_limit


def name_provider_quota_tool(tool: str) -> None:
    """Say which tool this request's spend belongs to, for the access-log line."""
    scope = _QUOTA_SCOPE.get()
    if scope is not None:
        scope.tool = tool


# --------------------------------------------------------------------------------------
# Credential resolution
# --------------------------------------------------------------------------------------


def _parse_env_file(path: Path) -> dict[str, str]:
    """Minimal ``KEY=VALUE`` .env parser -- stdlib only, no external dotenv dependency.

    Ignores blank lines and ``#`` comments; strips one layer of matching quotes. Returns
    an empty mapping (never raises) when the file does not exist.
    """
    values: dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return values
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key:
            values[key] = value
    return values


def resolve_credentials(
    *,
    env: dict[str, str] | None = None,
    user_config_env_file: Path | None = None,
    repo_env_file: Path | None = None,
) -> IntervalsCredentials | None:
    """Resolve intervals.icu credentials.

    Precedence, evaluated per key independently (a key present at an earlier tier is
    used as-is; a key missing there falls through to the next tier):

      1. the process environment -- works anywhere, including CI and hosted runs;
      2. ``~/.config/garmin-coach-loop/.env`` -- any user's per-machine config; works
         with no repository checked out at all, which is the point of the product path;
      3. the repo-root ``.env`` -- kept only for compatibility with a repo-checkout
         workflow. Never the only path: a fresh clone with no repo-root ``.env`` must
         still be configurable through tier 1 or 2.

    Returns ``None`` (never raises) when either credential cannot be resolved anywhere
    in the chain, so the caller can turn that into one explicit, honest block instead of
    a silent skip.
    """
    source_env = os.environ if env is None else env
    api_key = source_env.get(API_KEY_ENV_VAR)
    athlete_id = source_env.get(ATHLETE_ID_ENV_VAR)

    if not api_key or not athlete_id:
        user_config = _parse_env_file(
            user_config_env_file if user_config_env_file is not None else USER_CONFIG_ENV_PATH
        )
        api_key = api_key or user_config.get(API_KEY_ENV_VAR)
        athlete_id = athlete_id or user_config.get(ATHLETE_ID_ENV_VAR)

    if not api_key or not athlete_id:
        repo_config = _parse_env_file(repo_env_file if repo_env_file is not None else REPO_ROOT / ".env")
        api_key = api_key or repo_config.get(API_KEY_ENV_VAR)
        athlete_id = athlete_id or repo_config.get(ATHLETE_ID_ENV_VAR)

    if not api_key or not athlete_id:
        return None
    return IntervalsCredentials(api_key=api_key, athlete_id=athlete_id)


# --------------------------------------------------------------------------------------
# HTTP transport: GET only, one retry on URLError/HTTP 5xx, custom UA + Basic auth
# --------------------------------------------------------------------------------------


def _build_request(url: str, credentials: IntervalsCredentials) -> urllib.request.Request:
    request = urllib.request.Request(url, method="GET")
    request.add_header("Authorization", authorization_header(credentials))
    request.add_header("User-Agent", USER_AGENT)
    return request


def authorization_header(credentials: IntervalsCredentials) -> str:
    """Return the Intervals Authorization header value without logging it.

    ``bearer`` carries an OAuth access token; ``basic`` carries a personal API key. An
    unrecognized scheme fails closed rather than falling back to Basic, which would send
    an OAuth token as a password.
    """
    if credentials.auth_scheme == "bearer":
        # The OAuth access token is the credential itself; nothing is encoded around it.
        return f"Bearer {credentials.api_key}"
    if credentials.auth_scheme != "basic":
        raise ContextBuildError(
            f"unsupported intervals auth scheme: {credentials.auth_scheme!r}"
        )
    # Basic auth username is literally "API_KEY"; the API key itself is the password.
    token = base64.b64encode(f"API_KEY:{credentials.api_key}".encode("utf-8")).decode("ascii")
    return f"Basic {token}"


def _default_fetch(request: urllib.request.Request) -> ProviderResponse:
    with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:  # GET only
        return ProviderResponse(
            body=response.read(),
            rate_limit=_rate_limit_value(response.headers, "X-RateLimit-Limit"),
            rate_remaining=_rate_limit_value(response.headers, "X-RateLimit-Remaining"),
        )


class ProviderUnavailableError(ContextBuildError):
    """The provider could not answer this read: a network error or a 5xx, after the retry.

    A ``ContextBuildError`` like every other blocked step, so callers that catch the base
    class are unaffected. It is named apart for the one caller that lets a read fail --
    the optional wellness read in ``fetch_domain`` -- because that caller has to tell this
    apart from the other ways a read ends. This one is "the provider had a bad minute",
    and the next turn may well have it. Catching named classes rather than the base is
    what makes that an allow-list: a failure mode added later blocks until someone
    decides otherwise.
    """


class ProviderBudgetExhaustedError(ContextBuildError):
    """The provider is rate-limiting this application: an HTTP 429, after one bounded wait.

    Kept apart from an outage and from a permission failure because all three read the
    same to an athlete -- "it did not work" -- and mean three different things. The
    Intervals quota is granted per registered application, one pool shared by every
    connected athlete, so a 429 is never this athlete's connection being wrong and
    never something re-consenting fixes: it recovers on its own when the quota window
    rolls, and until then every athlete's turn is refused together (issue #260).
    Deliberately not caught by ``fetch_domain``'s optional-read allow-list: when the
    pool is dry, letting the turn limp on spends more of it for a degraded answer.
    """


class ProviderPermissionError(ContextBuildError):
    """The provider refused this read for what the connection is allowed to see: a 403.

    Kept apart from an outage because the fix is different and the athlete owns it: the
    Intervals consent page grants permissions separately, so a capability whose
    permission was never granted -- or was dropped on a later re-consent -- fails every
    turn, in the same way, until the connection is granted it again. Reporting that as a
    bad minute would tell the athlete to wait for something that is not coming back on
    its own.

    Distinct from a 401, which is the credential itself being refused rather than one
    capability, and which the gateway acts on by forgetting the connection.
    """


# The longest a 429's Retry-After is honoured for, in seconds. One bounded wait, not a
# backoff loop: past this, the caller is told the pool is dry rather than held on a
# socket the athlete is watching. Retry-After's HTTP-date form is treated as unusable
# on purpose -- a date means "much later", which is the refusal case, not the wait case.
RETRY_AFTER_CAP_SECONDS = 10


def _retry_after_seconds(headers: Any) -> int | None:
    raw = headers.get("Retry-After") if headers is not None else None
    if raw is None:
        return None
    try:
        seconds = int(str(raw).strip())
    except ValueError:
        return None
    return seconds if 0 < seconds <= RETRY_AFTER_CAP_SECONDS else None


def _budget_exhausted(cause: urllib.error.HTTPError) -> ProviderBudgetExhaustedError:
    return ProviderBudgetExhaustedError(
        "intervals.icu is rate-limiting this application (HTTP 429): the request "
        "budget is shared by every connected athlete, so this is not this "
        "connection's fault and reconnecting will not fix it. It recovers when "
        "the provider's quota window rolls.",
        upstream_status=429,
    )


def _fetch_with_retry(url: str, credentials: IntervalsCredentials, *, fetch: Fetcher) -> bytes:
    """One retry on URLError, HTTP 5xx, or a 429 whose Retry-After fits the cap.

    Any other HTTPError fails immediately. Every attempt is counted against the
    ambient quota scope -- a refused request spends the shared pool too -- and the
    latest quota headers are recorded whether the attempt succeeded or not.
    """
    last_error: Exception | None = None
    waited_for_budget = False
    for _attempt in range(2):
        count_provider_call()
        try:
            response = fetch(_build_request(url, credentials))
        except urllib.error.HTTPError as exc:
            last_error = exc
            note_provider_quota(exc.headers)
            if exc.code == 429:
                # The pool, not this athlete: wait once if the provider names a
                # short-enough delay and an attempt remains to spend it on,
                # otherwise refuse with the budget's own error.
                wait = (
                    None
                    if waited_for_budget or _attempt == 1
                    else _retry_after_seconds(exc.headers)
                )
                if wait is None:
                    raise _budget_exhausted(exc) from exc
                waited_for_budget = True
                time.sleep(wait)
                continue
            if exc.code < 500:
                # The status is carried, not just printed: a 401 or 403 here means this
                # athlete's credential was refused, which a caller can act on. 403 is
                # raised as its own class as well, because "this connection may not read
                # that" is a durable, athlete-fixable fact rather than a bad minute --
                # see ProviderPermissionError.
                error = (
                    ProviderPermissionError if exc.code == 403 else ContextBuildError
                )
                raise error(
                    f"intervals.icu request failed with HTTP {exc.code}",
                    upstream_status=exc.code,
                ) from exc
            # 5xx: fall through and retry once.
        except urllib.error.URLError as exc:
            last_error = exc
            # Network-level error: fall through and retry once.
        else:
            note_provider_quota_values(response.rate_limit, response.rate_remaining)
            return response.body
    raise ProviderUnavailableError(
        f"intervals.icu request failed after retry: {last_error}"
    ) from last_error


def _get_json(path_and_query: str, credentials: IntervalsCredentials, *, fetch: Fetcher) -> Any:
    url = BASE_URL.format(athlete_id=credentials.athlete_id) + path_and_query
    body = _fetch_with_retry(url, credentials, fetch=fetch)
    try:
        return json.loads(body)
    except json.JSONDecodeError as exc:
        raise ContextBuildError("intervals.icu returned invalid JSON") from exc


def _get_activity_json(
    activity_id: str, path: str, credentials: IntervalsCredentials, *, fetch: Fetcher
) -> Any:
    """Read one per-activity resource, which lives outside the athlete path."""
    url = ACTIVITY_URL.format(activity_id=activity_id) + path
    body = _fetch_with_retry(url, credentials, fetch=fetch)
    try:
        return json.loads(body)
    except json.JSONDecodeError as exc:
        raise ContextBuildError("intervals.icu returned invalid JSON") from exc


# --------------------------------------------------------------------------------------
# Endpoint readers
#
# Field names below were verified with live read-only GETs against the real account on
# 2026-08-10 (see /activities and /wellness response samples during development). They
# are hardcoded rather than rediscovered per request, matching the health.db mapping
# convention in source_personal_os.py.
# --------------------------------------------------------------------------------------


def _json_type_name(value: Any) -> str:
    """A short, safe label for a JSON value's shape -- never its content.

    Used only inside error messages, where naming *what kind of thing* came back
    ("object", "null", "string") is useful for diagnosis, but the value itself never is:
    it may carry a provider error body, or any other field this adapter must never
    surface (this file's docstring already guarantees the API key never leaks into an
    error; this extends the same guarantee to whatever the provider put in the body).
    """
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, dict):
        return "object"
    if isinstance(value, list):
        return "list"
    if isinstance(value, str):
        return "string"
    if isinstance(value, (int, float)):
        return "number"
    return type(value).__name__


def _require_json_list(payload: Any, *, endpoint: str) -> list[Any]:
    """Fail closed when a provider root is not the list shape both endpoints below
    are documented to return (archived issue #111).

    Invariant: intervals.icu documents both ``/activities`` and ``/wellness`` as
    returning a JSON array. Before this guard, a non-list root -- an object (e.g. an
    error envelope returned with HTTP 200, or a permission/schema change), ``null``, or
    a bare scalar -- was silently treated as ``[]`` by the two callers below, and
    ``fetch_domain`` then reports that as a successful "fresh" read of zero activities:
    indistinguishable from "the athlete did not train this window" even though the
    provider never actually answered the question asked. That is exactly the failure
    this file's fail-closed contract exists to prevent elsewhere (see ``_get_json``'s
    invalid-JSON guard, which this is the shape-level sibling of) and exactly what
    AGENTS.md's "treat missing/stale/partial/failed reads as unknown, never convert
    them to zero" rule forbids -- a root that is not even the documented JSON type is at
    least as untrustworthy as JSON that fails to parse at all, which already blocks.

    A warning is not enough: nothing downstream of ``SourceDomain`` has a channel that
    guarantees a caller reads a soft warning before treating
    ``freshness.activities == "fresh"`` plus zero actuals as ground truth for a
    coaching decision -- the whole context is consumed as one fact-checked structure,
    not a warnings log a human necessarily reads first.

    False-positive cost: none for a correctly functioning account. A genuine empty
    result -- nothing recorded in the window -- already arrives as ``[]``, which this
    function returns unchanged; only a response that itself violates the documented API
    contract raises. Every valid workflow (a fresh account, a quiet week, a request that
    fails outright and is handled by ``_fetch_with_retry`` above) keeps building a
    context exactly as before; only "200 OK with the wrong JSON type" newly blocks,
    which is the one case this issue exists to close.

    Only the JSON *type* of the offending root is ever named in the raised message --
    never the payload, never the URL (which embeds the athlete id), never a credential.
    """
    if isinstance(payload, list):
        return payload
    raise ContextBuildError(
        f"intervals.icu {endpoint} did not return a JSON list (got {_json_type_name(payload)})"
    )


def _fetch_activities(
    credentials: IntervalsCredentials,
    window: BuildWindow,
    *,
    fetch: Fetcher,
) -> tuple[list[dict[str, Any]], int]:
    """The 42-day cycle-planning activity window. Confirmed fields: id, type,
    start_date_local, moving_time (s), distance (m), average_speed (m/s),
    average_heartrate, paired_event_id,
    total_elevation_gain (m), and feel (1-5 athlete self-rating).

    Returns ``(rows, malformed_row_count)``. ``rows`` holds only dict-shaped list
    entries, exactly as before; a non-dict entry (a string, a number, ``null``, ...)
    inside an otherwise valid list is still excluded, but is now counted rather than
    disappearing with no trace, so broad row-schema drift cannot be reported as an
    unqualified fresh empty training history (archived issue #111)."""
    query = f"/activities?oldest={window.window42_start.isoformat()}&newest={window.window42_end.isoformat()}"
    payload = _get_json(query, credentials, fetch=fetch)
    rows = _require_json_list(payload, endpoint="/activities")
    parsed = [row for row in rows if isinstance(row, dict)]
    return parsed, len(rows) - len(parsed)


def _fetch_wellness(
    credentials: IntervalsCredentials,
    window: BuildWindow,
    *,
    fetch: Fetcher,
) -> tuple[list[dict[str, Any]], int]:
    """The 42-day recovery window. Confirmed fields: id (the date, e.g.
    "2026-08-09"), sleepSecs, sleepScore, hrv, restingHR. This account's Garmin health
    feed is effectively not flowing yet (confirmed live: sleepSecs/sleepScore/hrv were
    null on both rows returned; restingHR was present on only one of two) -- callers must
    treat missing fields as genuinely missing, never fabricate them.

    Returns ``(rows, malformed_row_count)`` -- see ``_fetch_activities`` above; the same
    counted-not-silently-dropped treatment applies to this endpoint's rows."""
    # The full cycle window, not the 7-day one the coverage and trend readings are
    # computed over. Those two keep their own span below; what this widening buys is the
    # daily values themselves, which the coach could not see at all while the read
    # stopped at seven days -- a three-night collapse in sleep four days before the
    # window opens is evidence the provider holds and this product never asked for
    # (issue #358). One request either way, and a wellness row is a handful of numbers.
    query = f"/wellness?oldest={window.window42_start.isoformat()}&newest={window.window_end.isoformat()}"
    payload = _get_json(query, credentials, fetch=fetch)
    rows = _require_json_list(payload, endpoint="/wellness")
    parsed = [row for row in rows if isinstance(row, dict)]
    return parsed, len(rows) - len(parsed)


def _fetch_run_sport_settings(
    credentials: IntervalsCredentials, *, fetch: Fetcher
) -> dict[str, Any] | None:
    """The athlete's Run sport-settings entry, or ``None`` when it could not be read.

    Optional supplementary evidence, never a required source: every failure -- network,
    auth, a shape the provider did not document, no Run entry at all -- degrades to
    ``None`` rather than raising, so a context build never blocks on it. Mirrors
    ``delivery.IntervalsTransport.run_sport_settings`` (verified live against the real
    account to carry Settings read access, now included by the requested
    ``SETTINGS:WRITE``, that this credential also uses for ``/activities`` and
    ``/wellness``), independently, for the context-building path
    rather than the delivery one.
    """
    try:
        payload = _get_json("/sport-settings", credentials, fetch=fetch)
    except ContextBuildError:
        return None
    if not isinstance(payload, list):
        return None
    for entry in payload:
        if isinstance(entry, dict) and "Run" in (entry.get("types") or []):
            return entry
    return None


def _run_sport_settings_max_hr(
    credentials: IntervalsCredentials, *, fetch: Fetcher
) -> float | None:
    """The max HR configured on the athlete's Run sport settings, or ``None``.

    One of the two sources a max-HR divergence report compares -- this file only reads
    it; ``context_core.assemble_context`` is the one place that puts it beside
    ``athlete_baseline.max_hr``. 0 is a sentinel, not a measurement, the same guard every
    other heart-rate field this module reads applies (see ``_map_segment``).
    """
    entry = _fetch_run_sport_settings(credentials, fetch=fetch)
    if entry is None:
        return None
    # Field name verified live 2026-08-16: the real /sport-settings Run entry carries
    # ``max_hr`` (alongside ``lthr`` and ``threshold_pace``, both already verified).
    value = _safe_float(entry.get("max_hr"))
    return value if value is not None and value > 0 else None


# --------------------------------------------------------------------------------------
# Mapping: raw API rows -> CoachContext pieces
# --------------------------------------------------------------------------------------


def _safe_feel(value: Any) -> int | None:
    """Parse intervals.icu's ``feel`` (1-5, athlete self-reported) into a strict int.

    CoachContext.recent_actuals[].subjective_feel requires an actual int, not a float
    (see validation._integer_or_null); a JSON number with no decimal point already
    parses as int, but this stays defensive in case the API ever emits ``3.0``. Anything
    else -- including an out-of-range value -- passes through unchanged rather than
    being guessed or clamped; validation is responsible for rejecting a bad value, not
    this mapper for silently making it look fine.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return None


def _paired_event_id(value: Any) -> str | None:
    """Normalize Intervals' event identity to the PlanState external-id type."""
    if value is None or isinstance(value, bool):
        return None
    text = str(value).strip()
    return text or None


def _activity_date(row: dict[str, Any]) -> dt.date | None:
    raw = row.get("start_date_local")
    if not isinstance(raw, str) or not raw:
        return None
    try:
        return dt.datetime.fromisoformat(raw).date()
    except ValueError:
        return None


def _wellness_date(row: dict[str, Any]) -> dt.date | None:
    raw = row.get("id")
    if not isinstance(raw, str):
        return None
    try:
        return dt.date.fromisoformat(raw)
    except ValueError:
        return None


# The running-family members of Strava's public API v3 `sport_type` enum
# (developers.strava.com/docs/reference, "SportType"), which intervals.icu's own `type`
# field mirrors: confirmed both by this repo's live account sample (`type:
# "WeightTraining"`, itself a member of that same enum) and by intervals.icu naming
# "TrailRun" as the type its own "Trail Run" activity label carries. There is no
# separate "Treadmill" member in that vocabulary -- an indoor treadmill run is still
# typed "Run" -- so it is deliberately not invented as its own case here.
_RUNNING_ACTIVITY_TYPES = frozenset({"run", "trailrun", "virtualrun"})

# The one strength-family member ever observed live for this account, and the one
# Strava/intervals.icu vocabulary member that means what this product means by
# "strength". Unlike the code this replaced, membership here is exact, not a "contains
# the substring 'strength'" test -- see _map_activity_sport's docstring below for why
# that distinction is the entire point of archived issue #111's fix.
_STRENGTH_ACTIVITY_TYPES = frozenset({"weighttraining"})

# The cross-training families, from the same Strava `sport_type` vocabulary the two sets
# above are drawn from. Deliberately conservative: an e-bike ride is left out because a
# motor changes what the duration means, and a Walk is not a Hike. A member left out is
# never lost -- it surfaces as `activity_type_excluded:<type>` in unknowns, and adding it
# later is one string here.
_CYCLING_ACTIVITY_TYPES = frozenset({"ride", "virtualride", "mountainbikeride", "gravelride"})
_SWIMMING_ACTIVITY_TYPES = frozenset({"swim", "openwaterswim"})
_HIKING_ACTIVITY_TYPES = frozenset({"hike"})
_ROWING_ACTIVITY_TYPES = frozenset({"rowing", "virtualrow"})

_SPORT_BY_ACTIVITY_TYPE = {
    **{member: "running" for member in _RUNNING_ACTIVITY_TYPES},
    **{member: "strength" for member in _STRENGTH_ACTIVITY_TYPES},
    **{member: "cycling" for member in _CYCLING_ACTIVITY_TYPES},
    **{member: "swimming" for member in _SWIMMING_ACTIVITY_TYPES},
    **{member: "hiking" for member in _HIKING_ACTIVITY_TYPES},
    **{member: "rowing" for member in _ROWING_ACTIVITY_TYPES},
}


def _map_activity_sport(activity_type: Any) -> str | None:
    """Map one provider activity ``type`` to this product's sport vocabulary, or
    ``None`` when the provider's type is not one this product acts on.

    A membership test against the two explicit vocabularies above -- never a substring
    or prefix test. archived issue #111: the code this replaced matched with
    ``str(activity_type).lower().startswith("run")``, which silently excluded
    "TrailRun" (it does not start with "run") from ``recent_actuals`` -- a completed
    trail run disappeared from training history with no trace. A membership test cannot
    make that mistake: a normalized type is either a named vocabulary member or it is
    not, regardless of where in the string anything sits.

    ``None`` covers two different things the caller must not conflate: a sport outside
    this product's vocabulary entirely (AlpineSki, Walk, ...), and a type string no
    vocabulary here recognizes at all (a future provider addition, a typo, a malformed
    value). Both stay excluded from ``recent_actuals``, but unlike before, making that
    exclusion observable is the caller's responsibility rather than a silent drop (see
    ``_build_recent_actuals``'s ``notes`` handling below).
    """
    return _SPORT_BY_ACTIVITY_TYPE.get(str(activity_type or "").strip().lower())


def _recorded_indoors(row: dict[str, Any]) -> bool | None:
    """Whether the recording device said this activity happened indoors.

    The provider's own ``trainer`` flag, read rather than guessed from ``type``.
    Verified live 2026-08-26 across six weeks of this account's runs: the flag is
    carried on every row, set on treadmill sessions and unset otherwise -- and set on
    one the provider typed plain ``Run`` as well as on the ones it typed
    ``VirtualRun``, so the type alone misses treadmill runs and this does not.

    Worth its place because a treadmill's distance is the machine's reading rather
    than a measurement, which makes a pace derived from it a different kind of number
    from a pace measured outdoors. Saying which kind this one is belongs here; what
    follows from it is the coach's to decide (AGENTS.md 4). No correction is applied
    and none could be -- how far a given treadmill is out is a property of that machine
    and that athlete, and a factor invented here would be invented precision.

    Three answers, not two. A row that carries the flag unset is a run the provider
    reported as not indoors; a row that carries no flag at all is one it said nothing
    about, and collapsing that into "outdoors" would be exactly the conversion
    AGENTS.md 3 forbids.
    """
    if "trainer" not in row:
        return None
    return row.get("trainer") is True


def _activity_type_label(raw_type: Any) -> str:
    """A short, stable label for an excluded activity's ``type``, for the
    ``activity_type_excluded`` note in ``_build_recent_actuals`` below.

    Never a raw provider body -- just the ``type`` string itself, which is normally a
    short enum-like token -- bounded defensively in case a future payload puts
    something unexpectedly large there."""
    if raw_type is None:
        return "missing"
    text = str(raw_type).strip()
    if not text:
        return "missing"
    return text[:40]


def _session_label(raw_name: Any) -> str | None:
    """A strength session's own name, or ``None`` when the provider carries none.

    Bounded the same way ``_activity_type_label`` is, and for the same reason: a name is
    normally a handful of characters the athlete typed, and nothing downstream should
    have to cope with a pathological one. An empty or whitespace name is ``None`` rather
    than an empty string -- the provider having no label is not a label.
    """
    if not isinstance(raw_name, str):
        return None
    text = raw_name.strip()
    return text[:80] if text else None


def _activity_coverage_days(activities: list[dict[str, Any]], window: BuildWindow) -> set[dt.date]:
    """Every distinct activity date in the 7-day window, regardless of mapped sport --
    mirrors source_personal_os's ``activity_days`` (any workout row counts toward
    coverage even if its type is later skipped for recent_actuals)."""
    days: set[dt.date] = set()
    for row in activities:
        day = _activity_date(row)
        if day is not None and window.window_start <= day <= window.window_end:
            days.add(day)
    return days


def _build_recent_actuals(
    activities: list[dict[str, Any]],
    window: BuildWindow,
    notes: list[str],
    threshold_sec_per_km: int | None,
) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    # Distinct excluded types only, reported once each after the loop below -- not one
    # note per activity. A sport this product does not act on (or a type-vocabulary
    # drift) is a per-type fact worth surfacing once, not a per-row flood that drowns
    # out everything else in `unknowns` for an athlete who, say, also logs bike rides.
    excluded_types: set[str] = set()
    for row in activities:
        day = _activity_date(row)
        if day is None or not (window.window42_start <= day <= window.window42_end):
            continue
        raw_type = row.get("type")
        sport = _map_activity_sport(raw_type)
        if sport is None:
            # archived issue #111: a type this vocabulary excludes (an unrelated sport, or one
            # genuinely unrecognized) must never look like the record was fully
            # understood and simply had nothing to report -- see _map_activity_sport's
            # docstring for the "unrelated sport vs unknown type" distinction this
            # deliberately does not need to make: both are observable the same way.
            excluded_types.add(_activity_type_label(raw_type))
            continue
        raw_id = row.get("id")
        if not raw_id:
            continue
        activity_id = f"intervals:{raw_id}"
        session_label: str | None = None
        if sport == "strength":
            # The sport is what this row is; how much of the body it worked and what
            # it cost to recover from are not things the record states. A pair of
            # constants here used to give a heavy leg day and an easy upper-body
            # session the same two labels, so null says the product did not judge it
            # -- duration, heart rate, stated feel and the athlete's own session name
            # below are what the coach reads instead (AGENTS.md 3, 4).
            adaptation, body_stress, cost = "strength", None, None
            # The one thing the provider knows about a strength session that nothing
            # else can supply. Verified live 2026-08-15 across this account's whole
            # strength history: `kg_lifted` is null on every one, `icu_lap_count` is 0,
            # and the streams are time and heart rate only -- so no exercise, no set and
            # no rep ever arrives from Garmin. What does arrive is the athlete's own name
            # for the session ("chest day", "back day"), and that is precisely the
            # grouping a coach would otherwise have to ask the athlete to restate.
            # Carried verbatim, never parsed into a category: reading "chest day" is the
            # coach's job, and a body-part lookup table here would be this product
            # guessing at a taxonomy it does not own (AGENTS.md 4).
            session_label = _session_label(row.get("name"))
        elif sport == "running":
            adaptation, cost = _classify_running(
                _safe_float(row.get("average_speed")), activity_id, notes, threshold_sec_per_km
            )
            body_stress = "lower"
        else:
            # A cross-training actual states nothing it does not know. The running
            # classifier reads pace against the athlete's *run* threshold, and a swim or
            # a ride pushed through it would arrive labelled with somebody else's
            # intensity; null is the honest value, and the sport, duration and heart
            # rate beside it are what the coach actually judges from (AGENTS.md 3, 4).
            adaptation = body_stress = cost = None
        moving_time = _safe_float(row.get("moving_time"))
        distance_m = _safe_float(row.get("distance"))
        average_speed = _safe_float(row.get("average_speed"))
        average_hr = _safe_float(row.get("average_heartrate"))
        duration_minutes = None
        if moving_time is not None and moving_time > 0:
            duration_minutes = max(1, round(moving_time / 60))
        candidates.append(
            {
                "activity_id": activity_id,
                "date": day.isoformat(),
                "sport": sport,
                "paired_event_id": _paired_event_id(row.get("paired_event_id")),
                "planned_session_id": None,
                "match_confidence": "unmatched",
                "adaptation": adaptation,
                "body_stress": body_stress,
                "cost": cost,
                "duration_minutes": duration_minutes,
                "distance_km": (
                    round(distance_m / 1000.0, 3)
                    if distance_m is not None and distance_m >= 0
                    else None
                ),
                "average_pace_sec_per_km": (
                    round(1000.0 / average_speed)
                    if average_speed is not None and average_speed > 0
                    else None
                ),
                "average_hr": average_hr if average_hr is not None and average_hr > 0 else None,
                "session_label": session_label,
                # Running only: on a lift the flag means nothing a coach reads, and on
                # a ride it would mean an indoor trainer, which is a different fact
                # this product has no consumer for yet.
                "recorded_indoors": _recorded_indoors(row) if sport == "running" else None,
                "completion": "completed",
                "elevation_gain_m": _safe_float(row.get("total_elevation_gain")),
                "subjective_feel": _safe_feel(row.get("feel")),
            }
        )
    for label in sorted(excluded_types):
        notes.append(f"activity_type_excluded:{label}")
    # Keep the bounded 42-day read intact. Cycle planning needs the full window; a
    # top-20 cap silently drops running evidence for athletes who lift most days.
    candidates.sort(key=lambda item: (item["date"], item["activity_id"]), reverse=True)
    recent = candidates
    recent.sort(key=lambda item: (item["date"], item["activity_id"]))
    return recent


def _segment_pace_sec_per_km(speed: float | None) -> int | None:
    return round(1000.0 / speed) if speed is not None and speed > 0 else None


def _map_segment(index: int, row: dict[str, Any]) -> dict[str, Any] | None:
    """Map one provider segment, keeping only fields a coach reads.

    The provider returns roughly eighty fields per segment, most of them null for a
    watch that does not measure them (power, lactate, core temperature, ...). Carrying
    them all would bury the four that decide whether a rep hit its target.

    Deliberately not filtered: segments that look like noise. This activity's own
    breakdown carries two 3-metre, 1-second entries, and a rule that drops them is a
    threshold this product would then own. A reader skips them at a glance; a
    hard-coded minimum silently deletes a genuinely short segment one day.
    """
    distance = _safe_float(row.get("distance"))
    moving_time = _safe_float(row.get("moving_time"))
    if distance is None and moving_time is None:
        return None
    average_hr = _safe_float(row.get("average_heartrate"))
    max_hr = _safe_float(row.get("max_heartrate"))
    min_hr = _safe_float(row.get("min_heartrate"))
    raw_type = row.get("type")
    return {
        "index": index,
        # What the provider called it. Not a claim that a WORK segment is the
        # prescribed work: on a real 5x1km this comes back with almost every segment
        # typed WORK, warm-up and recoveries included.
        "provider_type": raw_type if isinstance(raw_type, str) and raw_type else None,
        "distance_m": round(distance, 1) if distance is not None else None,
        "moving_time_sec": int(moving_time) if moving_time is not None else None,
        "average_pace_sec_per_km": _segment_pace_sec_per_km(_safe_float(row.get("average_speed"))),
        "average_hr": average_hr if average_hr is not None and average_hr > 0 else None,
        "max_hr": max_hr if max_hr is not None and max_hr > 0 else None,
        "min_hr": min_hr if min_hr is not None and min_hr > 0 else None,
        "elevation_gain_m": _safe_float(row.get("total_elevation_gain")),
    }


# What a segment keeps once the session is older than the full-detail window. Provider
# label, distance and elapsed time: between them they say how many repetitions were run,
# how long each took and how far each went, which is what a cycle review asks of a
# quality session from week one. Heart rate, pace and elevation are dropped rather than
# rounded -- pace is those two divided, and the rest answer a question nobody asks about
# a session four weeks old (issue #290). Order is the contract: it is written into every
# activity's own `segment_fields`.
SEGMENT_ROW_FIELDS = ("provider_type", "distance_m", "moving_time_sec")


def _fetch_activity_segments(
    activity_id: str, credentials: IntervalsCredentials, *, fetch: Fetcher
) -> list[dict[str, Any]]:
    """Read one activity's segment breakdown. Confirmed fields: type, distance (m),
    moving_time (s), average_speed (m/s), average/max/min_heartrate,
    total_elevation_gain (m), under the ``icu_intervals`` key.

    An activity the provider has not analyzed returns no segments rather than an
    error, which is why the caller treats an empty list as "nothing to report for this
    activity" and not as a failure.
    """
    payload = _get_activity_json(activity_id, "/intervals", credentials, fetch=fetch)
    if not isinstance(payload, dict):
        return []
    rows = payload.get("icu_intervals")
    if not isinstance(rows, list):
        return []
    mapped: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        segment = _map_segment(len(mapped), row)
        if segment is not None:
            mapped.append(segment)
    return mapped


def _build_segment_execution(
    activities: list[dict[str, Any]],
    window: BuildWindow,
    credentials: IntervalsCredentials,
    notes: list[str],
    *,
    fetch: Fetcher,
    structured_dates: frozenset[dt.date],
) -> dict[str, Any] | None:
    """Per-segment execution for recent runs, one provider read per activity.

    Scope is deliberately narrow, because this costs one request per activity, and
    every segment it returns is paid for by every later turn of the conversation it
    lands in (AGENTS.md 13):

    - running only. A strength entry carries no segments the provider can return,
      and per-set truth already arrives through ``strength_execution``.
    - two windows, not one, and neither is the 42-day one. Inside 14 days every
      segment is reported in full: the consumer is "how did the last hard session
      go", and that is read from the numbers. Between 14 and 28 days -- one cycle, which is the span a cycle
      review asks about -- the same session comes back as
      ``segment_rows`` -- one row per segment, carrying only what the provider called
      it, how far it went and how long it took. A cycle review on day 26 asks about
      week one, and ``recent_actuals`` cannot answer it: 44 minutes at 6:50/km reads
      the same whether three repetitions were run or five (issue #290). That review
      needs what the repetitions were and whether the same one keeps falling off; it
      does not need every heart rate that made them, and the full shape does not fit
      four weeks of them -- 3,701 characters against 537 for one 20-segment session,
      measured on the budget fixture.
    - days the plan prescribed more than one step on (``structured_dates``, issue
      #233). A run prescribed as one continuous effort -- "easy 40 minutes under 140
      bpm" -- is completely stated by the average pace and average heart rate
      ``recent_actuals`` already carries, and comes back from the provider as
      whatever auto-laps the watch cut, which is a reading of nothing. Reps are the
      case the whole-activity average cannot answer, and the case this field says it
      is for. Matching is by date rather than by paired event, so a second run on a
      prescribed-reps day is read too: over-reading is a wasted request, under-reading
      is a session the coach cannot review.

    Segments are reported exactly as the provider grouped them, in provider order,
    with no attempt to align them to the session's prescribed steps. That alignment
    looks obvious and is not: on 2026-08-14 a prescribed warm-up plus five reps plus a
    cool-down came back as fifteen segments, the warm-up split across two of them, and
    every segment but two typed WORK. Which segments are the work is a reading of the
    numbers, and readings belong to the coach (AGENTS.md 1).

    One activity failing does not fail the build: the others still report, and the
    failure is named in ``unknowns`` rather than looking like an activity with no
    segments.
    """
    entries: list[dict[str, Any]] = []
    failed = 0
    for row in activities:
        day = _activity_date(row)
        if day is None or not (window.window28_start <= day <= window.window28_end):
            continue
        if _map_activity_sport(row.get("type")) != "running":
            continue
        if day not in structured_dates:
            continue
        raw_id = row.get("id")
        if not raw_id:
            continue
        try:
            segments = _fetch_activity_segments(str(raw_id), credentials, fetch=fetch)
        except ContextBuildError:
            failed += 1
            continue
        if not segments:
            continue
        entry = {
            "activity_id": f"intervals:{raw_id}",
            "date": day.isoformat(),
            "sport": "running",
            # Repeated from the same activity's `recent_actuals` row rather than
            # left to be looked up there. This group is where the per-repetition
            # paces are, so it is where the kind of number they are has to be
            # legible; a reader who has to join two groups to find out is a reader
            # who will compare them to a prescribed pace without joining.
            "recorded_indoors": _recorded_indoors(row),
        }
        if day >= window.window14_start:
            entry["segments"] = segments
        else:
            # The same segments, in the same provider order, minus the fields a
            # four-week-old session is not reviewed on. `segment_fields` travels with
            # the rows rather than being implied by position alone, so the group stays
            # readable without this file open beside it.
            entry["segment_fields"] = list(SEGMENT_ROW_FIELDS)
            entry["segment_rows"] = [
                [segment.get(field) for field in SEGMENT_ROW_FIELDS]
                for segment in segments
            ]
        entries.append(entry)
    if failed:
        notes.append(f"segment_execution: {failed} activity segment read(s) failed")
    if not entries:
        return None
    entries.sort(key=lambda item: (item["date"], item["activity_id"]))
    return {
        "source": SOURCE_NAME,
        # Stated, not implied: a run outside this window was never read for segments,
        # which is a different fact from a run that was read and had none. The same
        # holds one level finer for a run inside it on a day nothing with reps was
        # prescribed -- see the docstring; the coach reads that run in recent_actuals.
        "window_start": window.window28_start.isoformat(),
        "window_end": window.window28_end.isoformat(),
        # Where the full shape stops and `segment_rows` begins. Without it, an
        # activity carrying rows reads as an activity whose heart rates the provider
        # did not return, which is a different fact and a worse one to act on.
        "full_detail_start": window.window14_start.isoformat(),
        "activities": entries,
    }


# --------------------------------------------------------------------------------------
# Within-session drift
#
# Everything above reports a session as one row of averages. A session's average is
# blind to the one thing that separates two sessions of identical duration: what
# happened between the start and the end. Two cases the averages cannot answer, both
# probed live on 2026-08-28 against the real account:
#
#   A run whose pace fell 2.6% while heart rate rose 4.3% and step length and ground
#   contact time held to within 1%. Cost paid by the circulation, not the legs -- but
#   the coach reads only "average 134 bpm at 8:23/km" and cannot see either half.
#
#   Two strength sessions the coach currently reads as the same seventy-odd minutes at
#   the same load. One went from 163s rest to 86s while set length went 26s to 62s; the
#   other went 163s to 142s while set length went 42s to 25s. Opposite sessions, one
#   summary.
#
# So what is carried here is the first third against the last third, and nothing else.
# No verdict, no threshold, no cause: whether a drift is heat, fatigue, or the session
# being built that way is the coach's reading (AGENTS.md 1, 11).
# --------------------------------------------------------------------------------------

# One provider request per activity, and the payload is the whole session, so these are
# the most expensive reads this module makes. Both are capped, both caps report what they
# dropped rather than silently truncating, and both are confined to the 14-day window --
# the consumer is "how did the last two weeks actually go", not a trend.
#
# Three, and the number is what the budget left rather than what training would pick.
# The question these answer is how the recent sessions ran, which is asked of this week
# far more often than the one before it, so the newest sessions are the ones worth the
# characters. Six put the whole context 585 over its ceiling and four still put a
# fully-attached cycle 599 over -- and the ceiling is the rule, not a target to raise: a
# new field is paid for out of the budget, never beside it. Three fits, covers most of a
# week at this athlete's frequency, and names every session it drops in unknowns, which
# is what keeps a capped read from looking like a quiet fortnight.
_MAX_DRIFT_ACTIVITIES = 3
_MAX_SET_STRUCTURE_ACTIVITIES = 3

# Requested by name, so the provider sends five series instead of the fourteen it holds.
# Heart rate and speed are the pair that says whether cost rose while output fell; step
# cadence and ground contact time are what separates a circulatory cost from a mechanical
# one. Anything the device did not record simply comes back absent.
#
# Step length is deliberately not among them: speed is cadence times step length, so a
# pace and a cadence already state it (504 s/km at 73 gives 815 mm, against 812 measured
# on the real account). Carrying it would spend characters on a value the two beside it
# determine -- and the budget is finite, so a derivable field is the first to go.
#
# Cadence earns its place by being the only one of the three an athlete can act on
# mid-run. Speed is cadence times step length, so a slower last third is one or the
# other giving way -- and "take quicker steps" is an instruction a runner can follow,
# while step length and ground contact time are measured consequences, not moves. On
# the real account on 2026-08-29, every one of the three runs that slowed lost more
# cadence than step length (one lost 2.1% cadence against 0.3% step length), so without
# this series the coach sees a run give way and cannot say what gave.
_DRIFT_STREAM_TYPES = ("heartrate", "velocity_smooth", "cadence", "stance_time")

# Below a quarter of an hour there is no drift to read: a heat or fatigue cost needs time
# to develop, and thirds of a ten-minute run differ by warm-up, not by anything the coach
# would act on. Samples are one per second on this device.
_MIN_DRIFT_SAMPLES = 900


def _get_activity_bytes(
    activity_id: str, path: str, credentials: IntervalsCredentials, *, fetch: Fetcher
) -> bytes:
    """Read one per-activity resource that is not JSON, and return it unparsed.

    Used for the original uploaded file. The bytes are handed straight to a reader
    that returns a handful of integers and drops them; nothing here or downstream
    stores the file (AGENTS.md 2).
    """
    url = ACTIVITY_URL.format(activity_id=activity_id) + path
    return _fetch_with_retry(url, credentials, fetch=fetch)


def _thirds_mean(values: list[float]) -> tuple[float, float] | None:
    """Mean of the first and last third, or ``None`` when the series is too short.

    Three is the smallest length at which the two ends are disjoint. Shorter than
    that and both ends would read the same samples, so any difference between them
    would be an artefact of the arithmetic.
    """
    if len(values) < 3:
        return None
    size = len(values) // 3
    return sum(values[:size]) / size, sum(values[-size:]) / size


def _numeric_samples(stream: Any) -> list[float]:
    """The numeric samples of one stream, with gaps dropped rather than zeroed.

    A paused or unrecorded second arrives as ``null``. Reading it as 0 would drag an
    average toward a value the athlete never produced, which is the conversion
    AGENTS.md 3 forbids.
    """
    if not isinstance(stream, list):
        return []
    return [
        float(sample)
        for sample in stream
        if isinstance(sample, (int, float)) and not isinstance(sample, bool)
    ]


def _fetch_activity_streams(
    activity_id: str, credentials: IntervalsCredentials, *, fetch: Fetcher
) -> dict[str, list[Any]]:
    """Read the named per-sample series for one activity, keyed by stream type."""
    query = "/streams?types=" + ",".join(_DRIFT_STREAM_TYPES)
    payload = _get_activity_json(activity_id, query, credentials, fetch=fetch)
    if not isinstance(payload, list):
        return {}
    streams: dict[str, list[Any]] = {}
    for row in payload:
        if not isinstance(row, dict):
            continue
        name = row.get("type")
        data = row.get("data")
        if isinstance(name, str) and isinstance(data, list):
            streams[name] = data
    return streams


def _drift_ends(streams: dict[str, list[Any]]) -> tuple[dict[str, Any], dict[str, Any]] | None:
    """The four measurements at each end of the run, or ``None`` if it is too short.

    Every series is thirded on its own samples rather than on a shared index. A device
    that recorded ground contact time for only part of a run still reports the part it
    has, and a series it never recorded is simply absent from both ends -- neither is
    filled in, and neither costs the other three their reading.
    """
    heart_rate = _numeric_samples(streams.get("heartrate"))
    if len(heart_rate) < _MIN_DRIFT_SAMPLES:
        return None

    first: dict[str, Any] = {}
    last: dict[str, Any] = {}

    def carry(key: str, ends: tuple[float, float] | None, convert) -> None:
        if ends is None:
            return
        start, finish = convert(ends[0]), convert(ends[1])
        if start is None or finish is None:
            return
        first[key], last[key] = start, finish

    carry("average_hr", _thirds_mean(heart_rate), lambda value: round(value))
    carry(
        "average_pace_sec_per_km",
        _thirds_mean([sample for sample in _numeric_samples(streams.get("velocity_smooth")) if sample > 0]),
        _segment_pace_sec_per_km,
    )
    carry("average_cadence_spm", _thirds_mean(_numeric_samples(streams.get("cadence"))), lambda value: round(value))
    carry("stance_time_ms", _thirds_mean(_numeric_samples(streams.get("stance_time"))), lambda value: round(value))

    if "average_hr" not in first:
        return None
    return first, last


def _build_run_drift(
    activities: list[dict[str, Any]],
    window: BuildWindow,
    credentials: IntervalsCredentials,
    notes: list[str],
    *,
    fetch: Fetcher,
) -> dict[str, Any] | None:
    """First third against last third for recent runs, one provider read each.

    Every run in the 14-day window is eligible, not only the structured ones that
    ``segment_execution`` reads. The case this answers is loudest on an easy run:
    a steady effort is exactly where a rising heart rate at a falling pace means
    something, and where the session's own average hides it completely.
    """
    candidates: list[tuple[dt.date, str]] = []
    for row in activities:
        day = _activity_date(row)
        if day is None or not (window.window14_start <= day <= window.window14_end):
            continue
        if _map_activity_sport(row.get("type")) != "running":
            continue
        raw_id = row.get("id")
        if raw_id:
            candidates.append((day, str(raw_id)))

    candidates.sort(reverse=True)
    dropped = max(0, len(candidates) - _MAX_DRIFT_ACTIVITIES)
    if dropped:
        # Named, never silent: a capped read that reports nothing reads exactly like a
        # window in which the athlete ran six times.
        notes.append(f"run_drift: {dropped} older run(s) in the window were not read")

    entries: list[dict[str, Any]] = []
    failed = 0
    for day, activity_id in candidates[:_MAX_DRIFT_ACTIVITIES]:
        try:
            streams = _fetch_activity_streams(activity_id, credentials, fetch=fetch)
        except ContextBuildError:
            failed += 1
            continue
        ends = _drift_ends(streams)
        if ends is None:
            continue
        entries.append(
            {
                "activity_id": f"intervals:{activity_id}",
                "date": day.isoformat(),
                "first_third": ends[0],
                "last_third": ends[1],
            }
        )

    if failed:
        notes.append(f"run_drift: {failed} activity stream read(s) failed")
    if not entries:
        return None
    entries.sort(key=lambda item: (item["date"], item["activity_id"]))
    return {
        "source": SOURCE_NAME,
        "window_start": window.window14_start.isoformat(),
        "window_end": window.window14_end.isoformat(),
        "activities": entries,
    }


def _build_set_structure(
    activities: list[dict[str, Any]],
    window: BuildWindow,
    credentials: IntervalsCredentials,
    notes: list[str],
    *,
    fetch: Fetcher,
) -> dict[str, Any] | None:
    """Set structure for recent strength sessions, read out of the original upload.

    This is the only field in this module that does not come from a provider-parsed
    value. Intervals stores the file the watch uploaded and parses none of its set
    messages, so the activity endpoint has no set count, no set length and no rest at
    all -- see ``fit_sets`` for what was probed and what is deliberately left behind.

    A session whose file cannot be parsed is reported as a failure in ``unknowns``
    rather than as a session with no sets, because those are different facts: the
    second one is what a strength activity started but never stepped through looks
    like, and it legitimately returns nothing.
    """
    candidates: list[tuple[dt.date, str]] = []
    for row in activities:
        day = _activity_date(row)
        if day is None or not (window.window14_start <= day <= window.window14_end):
            continue
        if _map_activity_sport(row.get("type")) != "strength":
            continue
        raw_id = row.get("id")
        if raw_id:
            candidates.append((day, str(raw_id)))

    candidates.sort(reverse=True)
    dropped = max(0, len(candidates) - _MAX_SET_STRUCTURE_ACTIVITIES)
    if dropped:
        notes.append(
            f"set_structure: {dropped} older strength session(s) in the window were not read"
        )

    entries: list[dict[str, Any]] = []
    failed = 0
    for day, activity_id in candidates[:_MAX_SET_STRUCTURE_ACTIVITIES]:
        try:
            payload = _get_activity_bytes(activity_id, "/file", credentials, fetch=fetch)
        except ContextBuildError:
            failed += 1
            continue
        try:
            summary = summarise_sets(payload)
        except FitParseError:
            failed += 1
            continue
        if summary is None:
            continue
        entries.append(
            {
                "activity_id": f"intervals:{activity_id}",
                "date": day.isoformat(),
                **summary,
            }
        )

    if failed:
        notes.append(f"set_structure: {failed} strength file read(s) failed")
    if not entries:
        return None
    entries.sort(key=lambda item: (item["date"], item["activity_id"]))
    return {
        "source": SOURCE_NAME,
        "window_start": window.window14_start.isoformat(),
        "window_end": window.window14_end.isoformat(),
        "activities": entries,
    }


_RECOVERY_FIELDS = ("sleepScore", "hrv", "restingHR")


def _wellness_field_values(
    wellness: list[dict[str, Any]], window: BuildWindow, field: str
) -> dict[dt.date, float]:
    """Real values of one wellness field, by date, inside the 7-day coverage window.

    0 is a sentinel, not a measurement -- no living athlete has a resting HR, HRV, or
    sleep score of zero, so 0 must never count as evidence. Factored out (issue #95) so
    every reader of a field -- freshness grading, coverage counts, ``last_observed``, and
    trend calculation -- shares one answer to "does this day carry a real value" instead
    of each re-deriving it and risking disagreement: before this, freshness and
    coverage/trends each scanned wellness rows separately, and only a shared comment
    kept their "0 is a sentinel" handling in sync.
    """
    values: dict[dt.date, float] = {}
    for row in wellness:
        day = _wellness_date(row)
        if day is None or not (window.window_start <= day <= window.window_end):
            continue
        value = _safe_float(row.get(field))
        if value is not None and value > 0:
            values[day] = value
    return values


# Provider key -> `recovery_signals_day` key. Intervals carries raw RMSSD in ms and no
# Garmin-derived status, average or score, so only these four of that shape's fields can
# ever be filled from this source; every other key stays absent, which the shape reads as
# not observed rather than as a zero.
_RECOVERY_DAY_FIELDS = (
    ("sleepScore", "sleep_score"),
    ("sleepSecs", "sleep_duration_sec"),
    ("hrv", "hrv_last_night_ms"),
    ("restingHR", "resting_hr_bpm"),
)


def _recovery_days(
    wellness: list[dict[str, Any]], window: BuildWindow
) -> list[dict[str, Any]]:
    """Every day in the 42-day window that carries at least one real recovery value.

    The values themselves, in the same per-day shape a local health database and a client
    upload already produce, so one container holds all three origins and the coach reads
    one thing. Nothing here is graded, averaged or compared: the trend and coverage
    readings above are computed separately and still over their own 7-day window.

    A day with no real value is omitted rather than written as a row of nulls -- the
    wellness endpoint returns a row for every date whether or not anything was measured,
    and a padded row would report an unmeasured day as an observed one. "Real" is
    ``_wellness_field_values``'s rule: 0 is a sentinel, never a measurement.
    """
    days: dict[dt.date, dict[str, Any]] = {}
    for row in wellness:
        day = _wellness_date(row)
        if day is None or not (window.window42_start <= day <= window.window_end):
            continue
        readings = {
            key: value
            for source_key, key in _RECOVERY_DAY_FIELDS
            if (value := _safe_float(row.get(source_key))) is not None and value > 0
        }
        if readings:
            days[day] = {"date": day.isoformat(), **readings}
    return [days[day] for day in sorted(days)]


def _last_observed_iso(values: dict[dt.date, float]) -> str | None:
    """The latest date in a per-day value mapping, as an ISO string, or None if empty."""
    return max(values).isoformat() if values else None


def _recovery_freshness(wellness: list[dict[str, Any]], window: BuildWindow) -> str:
    """Grade the wellness feed's recency -- a mechanical fact, not a coaching judgment.

    The wellness endpoint returns a row for a day even when nothing was measured (every
    field null), so a successful GET -- and even a recent row date -- proves nothing
    about recovery evidence. What this grade reports instead is how current the newest
    observed *signal* value is, per field (see ``_wellness_field_values``):

      - no field has any real value anywhere in the window        -> "failed"
      - some field's latest real value is <=1 day old (vs window_end) -> "fresh"
      - some field has a real value, but none of them that recent -> "stale"

    That is the whole grade. Before issue #95 this function also decided whether a
    single current signal was *enough* to lean on -- a "partial" tier sitting between
    stale and fresh -- which is a training judgment, and the deterministic layer is the
    wrong place for it: the model had no per-signal dates anywhere in the context to make
    that call itself. Sufficiency is the coach's judgment now, read from ``coverage``
    (each signal's ``observed_days`` and ``last_observed``, both per issue #95) and
    ``recovery_trends`` (per-signal direction) -- both carry the per-signal detail this
    field deliberately discards.
    """
    latest_dates: list[dt.date] = []
    for field in _RECOVERY_FIELDS:
        values = _wellness_field_values(wellness, window, field)
        if values:
            latest_dates.append(max(values))
    if not latest_dates:
        return "failed"
    if any((window.window_end - day).days <= 1 for day in latest_dates):
        return "fresh"
    return "stale"


def _build_recovery_domain(
    wellness: list[dict[str, Any]],
    window: BuildWindow,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any]]:
    """Return (coverage_sleep, coverage_hrv, coverage_resting_hr, recovery_trends).

    Sleep uses sleepScore (same +/-10-point median logic as the personal-os sleep-percent
    trend). HRV uses +/-10% of the window median -- there is no Garmin baseline JSON on
    this path, unlike personal-os's HRV trend. Resting HR shares the same median logic
    as sleep. Each coverage entry's ``last_observed`` is the newest date inside the
    window that field carried a real value -- an acquisition fact the coach reads
    coverage for, never a verdict on whether it is recent enough (issue #95; that verdict
    used to live in ``_recovery_freshness``'s now-removed "partial" tier).
    """
    sleep_values = _wellness_field_values(wellness, window, "sleepScore")
    hrv_values = _wellness_field_values(wellness, window, "hrv")
    resting_values = _wellness_field_values(wellness, window, "restingHR")

    recovery_trends = {
        "sleep": _median_trend(sleep_values, window.window_end, band_points=10.0),
        "hrv": _median_trend(hrv_values, window.window_end, band_fraction=0.10),
        "resting_hr": _median_trend(resting_values, window.window_end, band_points=10.0),
    }
    coverage_sleep = coverage_entry(len(sleep_values))
    coverage_sleep["last_observed"] = _last_observed_iso(sleep_values)
    coverage_hrv = coverage_entry(len(hrv_values))
    coverage_hrv["last_observed"] = _last_observed_iso(hrv_values)
    coverage_resting_hr = coverage_entry(len(resting_values))
    coverage_resting_hr["last_observed"] = _last_observed_iso(resting_values)
    return (
        coverage_sleep,
        coverage_hrv,
        coverage_resting_hr,
        recovery_trends,
    )


# --------------------------------------------------------------------------------------
# Public entry point
# --------------------------------------------------------------------------------------


def fetch_recent_activity(
    credentials: IntervalsCredentials,
    window: BuildWindow,
    *,
    fetch: Fetcher | None = None,
) -> RecentActivity:
    """Read the cycle-planning activity window on its own -- one GET, ``/activities``.

    For the caller that reports what the provider has been holding all along and nothing
    a plan would be needed to give meaning to. ``/wellness`` and ``/sport-settings`` are
    not requested, because nothing on that path reads either back: the coverage entries
    and trends built from wellness would be built and dropped, and the sport settings'
    max HR has no ``athlete_baseline.max_hr`` to be compared against until a PlanState
    exists to carry one.

    Raises ``ContextBuildError`` on any auth or network failure, exactly as
    ``fetch_domain`` does. What a failed read means for the response is the caller's
    decision, not this function's.

    Run intensity is classified unanchored here. There is no PlanState, so there is no
    threshold pace to classify against, and unmatched runs stay at the easy floor the
    same way ``fetch_domain`` leaves them when the baseline carries no threshold.
    Neither kind of note that produces travels back -- not the per-run classification
    notes, which on this path would all say the same thing about every run, and not the
    count of rows the provider returned in a shape this code cannot parse. The pre-plan
    view has never carried either; ``fetch_domain`` reports both from the first context
    build onward, and widening that is a change to what the first turn *says*, not to
    what it costs.
    """
    active_fetch = fetch if fetch is not None else _default_fetch
    activities, _ = _fetch_activities(credentials, window, fetch=active_fetch)
    discarded_notes: list[str] = []
    return RecentActivity(
        # _build_recent_actuals reads the whole 42-day window, the same span fetch_domain
        # reports, so the two paths cannot disagree about how far back was searched.
        actuals_window_start=window.window42_start,
        activity_days=frozenset(_activity_coverage_days(activities, window)),
        recent_actuals=_build_recent_actuals(activities, window, discarded_notes, None),
    )


def fetch_domain(
    credentials: IntervalsCredentials,
    window: BuildWindow,
    *,
    fetch: Fetcher | None = None,
    threshold_sec_per_km: int | float | None = None,
    structured_dates: frozenset[dt.date] = frozenset(),
    baseline_max_hr: Any = None,
) -> SourceDomain:
    """Fetch and map one CoachContext activity/recovery domain from intervals.icu.

    Raises ``ContextBuildError`` when the *activity* read fails (after one retry on
    URLError/HTTP 5xx) -- never returns a partial or fabricated domain, and never falls
    back to anything else; that decision belongs to the caller, not this function.

    The wellness read is the exception, and it is one the caller could not make for
    itself. Two of the ways it can end return a domain whose recovery half is unread and
    says which one it was -- ``freshness_recovery`` "unknown", every coverage entry
    empty, and in ``extra_unknowns`` either ``intervals_wellness_read_failed``
    (``ProviderUnavailableError``: the provider had a bad minute) or
    ``intervals_wellness_permission_denied`` (``ProviderPermissionError``: the connection
    may not read wellness until the athlete grants it again). Nothing is fabricated by
    that; what would be fabricated is the alternative, where zero observed days from a
    read that never happened is indistinguishable from zero the provider reported -- or
    where a permission the athlete can restore reads as weather.

    Only those two degrade. A 401, which is the credential itself refused rather than one
    capability, and a body this code cannot parse both still raise from the wellness read
    exactly as they do from the activity one.

    Freshness is asymmetric between the two domains on purpose. Activities:
    "fresh" on read success, because activity sync is near-real-time and an empty
    window means the athlete did not train, not that the pipe is behind. Recovery:
    graded from observed signal values per field (see ``_recovery_freshness``),
    because the wellness endpoint returns rows even for unmeasured days and a
    successful GET of a value-empty feed is exactly the case where claiming
    "fresh" lets a decision pretend it has recovery evidence. Per-day detail
    stays in coverage and recovery_trends. Doctor check: the activity read doubles
    as the authenticated-GET doctor probe (already required for real data; a
    dedicated ``/profile`` call was verified live as an alternative but is redundant
    here). The wellness read used to share that job and no longer can -- it is
    allowed to come back unread -- so ``doctor_status`` "passed" now says the
    credential was accepted on the read that blocks, which is the one it has to be
    true of. ``threshold_sec_per_km`` (from the current PlanState's
    athlete_baseline) anchors unmatched-run intensity classification to the
    athlete's own threshold; without it unmatched runs stay unclassified at the
    easy floor. ``structured_dates`` are the days the plan prescribed more than one
    step on, and they bound the per-segment read -- see ``_build_segment_execution``;
    an empty set means no segments are read at all, which is what a caller with no
    plan in hand should get rather than every run in the window.
    ``baseline_max_hr`` is the PlanState figure the read of the provider's own Run
    sport settings exists to be compared against, and the whole reason that request is
    or is not made -- see below.
    """
    active_fetch = fetch if fetch is not None else _default_fetch
    activities, activities_malformed_rows = _fetch_activities(credentials, window, fetch=active_fetch)
    # The recovery half is optional evidence and its read is allowed to fail. Activities
    # are not: matching, the cycle record and baseline evidence all run on them, so a
    # turn without them has nothing to reconcile against and refusing is honest. A
    # wellness outage is a different fact -- the athlete still has a plan, a week and a
    # today -- and ending the turn on it answered "what should I do today?" with a
    # provider error. What must not happen is the unread feed reading as a measured one,
    # or two different reasons for it reading as one: `wellness_unread` carries both
    # facts to every field built from it below.
    #
    # Only the two named classes are let through. A 401 -- the credential itself refused,
    # which the gateway answers by forgetting the connection -- and a body this code
    # cannot parse still block from the wellness read exactly as they do from the
    # activity one, and so does any failure a later change invents.
    wellness_unread: str | None = None
    try:
        wellness, wellness_malformed_rows = _fetch_wellness(
            credentials, window, fetch=active_fetch
        )
    except ProviderUnavailableError:
        wellness, wellness_malformed_rows = [], 0
        wellness_unread = "intervals_wellness_read_failed"
    except ProviderPermissionError:
        wellness, wellness_malformed_rows = [], 0
        wellness_unread = (
            "intervals_wellness_permission_denied: the Intervals connection is not "
            "permitted to read wellness; recovery evidence stays unavailable until it "
            "is reconnected with that permission"
        )

    activity_dates = {day for day in (_activity_date(row) for row in activities) if day is not None}
    wellness_dates = {day for day in (_wellness_date(row) for row in wellness) if day is not None}
    all_dates = activity_dates | wellness_dates
    data_through = max(all_dates).isoformat() if all_dates else None

    source_entry = {
        "source": SOURCE_NAME,
        "mode": "direct_rest_readonly",
        "doctor_status": "passed",
        "observed_at": window.now_iso,
        "data_through": data_through,
        "sanitized": True,
    }

    activity_days = _activity_coverage_days(activities, window)
    coverage_sleep, coverage_hrv, coverage_resting_hr, recovery_trends = _build_recovery_domain(wellness, window)

    notes: list[str] = []
    # Row-schema drift must not be reportable as an unqualified fresh empty training or
    # wellness history (archived issue #111): a non-dict row inside an otherwise-valid list is
    # still dropped, exactly as before, but the drop is now counted here so a broad
    # schema change is visible in `unknowns` instead of looking identical to "nothing to
    # report".
    if activities_malformed_rows:
        notes.append(f"intervals_activities_malformed_rows:{activities_malformed_rows}")
    if wellness_malformed_rows:
        notes.append(f"intervals_wellness_malformed_rows:{wellness_malformed_rows}")
    # The one place the difference is stated in words. Coverage says nothing was
    # observed, which is also what a provider that answered with an empty feed
    # produces; this says the feed was never read and why, so nothing about the
    # athlete's recovery can be concluded from those zeros in either direction, and a
    # permission the athlete can restore does not read as weather.
    if wellness_unread is not None:
        notes.append(wellness_unread)
    recent_actuals = _build_recent_actuals(activities, window, notes, threshold_sec_per_km)
    segment_execution = _build_segment_execution(
        activities, window, credentials, notes, fetch=active_fetch,
        structured_dates=structured_dates,
    )
    run_drift = _build_run_drift(activities, window, credentials, notes, fetch=active_fetch)
    set_structure = _build_set_structure(
        activities, window, credentials, notes, fetch=active_fetch
    )
    # One more request, same credential, same read-only GET: the Run sport settings'
    # own max HR, so a later divergence check has both sides to compare (see
    # context_core._max_hr_divergence_note). Never blocks the build -- see
    # _fetch_run_sport_settings for why every failure degrades to None instead.
    #
    # Made only when the caller has a measured figure for it to disagree with. That note
    # is the value's one and only reader, and it reports nothing unless both sides are
    # measured numbers, so with no baseline max HR this request cannot move a single
    # field of the context it would be spent on -- it can only be read and thrown away.
    # The guard is ``_measured_number`` itself, imported from the note rather than
    # restated here, because a gate that merely resembled the note's own test could
    # drift into either of the two failures that matter: a request whose answer is
    # discarded, or a comparison missing a side it could have had.
    sport_settings_max_hr = (
        _run_sport_settings_max_hr(credentials, fetch=active_fetch)
        if _measured_number(baseline_max_hr)
        else None
    )

    return SourceDomain(
        sources=[source_entry],
        freshness_activities="fresh",
        # "unknown" is the grade no successful read can produce: `_recovery_freshness`
        # answers fresh, stale or failed, and "failed" already means the feed answered
        # and carried no value anywhere in the window. An unread feed has to be its own
        # grade, or the two collapse into one and the coach cannot tell a provider
        # outage from a week the athlete measured nothing.
        freshness_recovery=(
            "unknown" if wellness_unread else _recovery_freshness(wellness, window)
        ),
        # _build_recent_actuals reads the whole 42-day window and caps nothing, so every
        # session of a cycle was searched for an attachment.
        actuals_window_start=window.window42_start,
        activity_days=frozenset(activity_days),
        coverage_sleep=coverage_sleep,
        coverage_hrv=coverage_hrv,
        coverage_resting_hr=coverage_resting_hr,
        recovery_trends=recovery_trends,
        recent_actuals=recent_actuals,
        segment_execution=segment_execution,
        run_drift=run_drift,
        set_structure=set_structure,
        sport_settings_max_hr=sport_settings_max_hr,
        extra_unknowns=list(notes),
        recovery_days=_recovery_days(wellness, window),
    )
