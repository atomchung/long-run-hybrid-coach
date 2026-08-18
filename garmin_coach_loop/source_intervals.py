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
import datetime as dt
import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .context_core import (
    BuildWindow,
    ContextBuildError,
    SourceDomain,
    _classify_running,
    coverage_entry,
    _median_trend,
    _safe_float,
)
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


# A callable that performs one GET given a fully-prepared Request and returns the raw
# response body. The default implementation is real urllib; tests inject a fake so the
# unit suite never touches the network.
Fetcher = Callable[[urllib.request.Request], bytes]


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


def _default_fetch(request: urllib.request.Request) -> bytes:
    with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:  # GET only
        return response.read()


def _fetch_with_retry(url: str, credentials: IntervalsCredentials, *, fetch: Fetcher) -> bytes:
    """One retry on URLError or HTTP 5xx; any other HTTPError fails immediately."""
    last_error: Exception | None = None
    for _attempt in range(2):
        try:
            return fetch(_build_request(url, credentials))
        except urllib.error.HTTPError as exc:
            last_error = exc
            if exc.code < 500:
                # The status is carried, not just printed: a 401 or 403 here means this
                # athlete's credential was refused, which a caller can act on.
                raise ContextBuildError(
                    f"intervals.icu request failed with HTTP {exc.code}",
                    upstream_status=exc.code,
                ) from exc
            # 5xx: fall through and retry once.
        except urllib.error.URLError as exc:
            last_error = exc
            # Network-level error: fall through and retry once.
    raise ContextBuildError(f"intervals.icu request failed after retry: {last_error}") from last_error


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
    are documented to return (issue #111).

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
    unqualified fresh empty training history (issue #111)."""
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
    """The 7-day coverage/trends window. Confirmed fields: id (the date, e.g.
    "2026-08-09"), sleepSecs, sleepScore, hrv, restingHR. This account's Garmin health
    feed is effectively not flowing yet (confirmed live: sleepSecs/sleepScore/hrv were
    null on both rows returned; restingHR was present on only one of two) -- callers must
    treat missing fields as genuinely missing, never fabricate them.

    Returns ``(rows, malformed_row_count)`` -- see ``_fetch_activities`` above; the same
    counted-not-silently-dropped treatment applies to this endpoint's rows."""
    query = f"/wellness?oldest={window.window_start.isoformat()}&newest={window.window_end.isoformat()}"
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
    account to carry the ``SETTINGS:READ`` scope this same credential already uses for
    ``/activities`` and ``/wellness``), independently, for the context-building path
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
# that distinction is the entire point of issue #111's fix.
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
    or prefix test. Issue #111: the code this replaced matched with
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
            # Issue #111: a type this vocabulary excludes (an unrelated sport, or one
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
            adaptation, body_stress, cost = "strength", "full", "moderate"
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
) -> dict[str, Any] | None:
    """Per-segment execution for recent runs, one provider read per activity.

    Scope is deliberately narrow, because this costs one request per activity and the
    42-day window holds far more than a coach reads before writing a week:

    - running only. A strength entry carries no segments the provider can return,
      and per-set truth already arrives through ``strength_execution``.
    - the 14-day window, not the 42-day one. The consumer is "how did the last hard
      session go", which is a question about the last week or two. A cycle review
      reads trends, and trends are what ``recent_actuals`` already carries.

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
        if day is None or not (window.window14_start <= day <= window.window14_end):
            continue
        if _map_activity_sport(row.get("type")) != "running":
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
        entries.append(
            {
                "activity_id": f"intervals:{raw_id}",
                "date": day.isoformat(),
                "sport": "running",
                "segments": segments,
            }
        )
    if failed:
        notes.append(f"segment_execution: {failed} activity segment read(s) failed")
    if not entries:
        return None
    entries.sort(key=lambda item: (item["date"], item["activity_id"]))
    return {
        "source": SOURCE_NAME,
        # Stated, not implied: a run outside this window was never read for segments,
        # which is a different fact from a run that was read and had none.
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


def fetch_domain(
    credentials: IntervalsCredentials,
    window: BuildWindow,
    *,
    fetch: Fetcher | None = None,
    threshold_sec_per_km: int | float | None = None,
) -> SourceDomain:
    """Fetch and map one CoachContext activity/recovery domain from intervals.icu.

    Raises ``ContextBuildError`` on any auth or network failure (after one retry on
    URLError/HTTP 5xx) -- never returns a partial or fabricated domain, and never falls
    back to anything else; that decision belongs to the caller, not this function.

    Freshness is asymmetric between the two domains on purpose. Activities:
    "fresh" on read success, because activity sync is near-real-time and an empty
    window means the athlete did not train, not that the pipe is behind. Recovery:
    graded from observed signal values per field (see ``_recovery_freshness``),
    because the wellness endpoint returns rows even for unmeasured days and a
    successful GET of a value-empty feed is exactly the case where claiming
    "fresh" lets a decision pretend it has recovery evidence. Per-day detail
    stays in coverage and recovery_trends. Doctor check: the wellness and
    activities reads themselves
    double as the authenticated-GET doctor probe (both already required for real
    data; a dedicated ``/profile`` call was verified live as an alternative but is
    redundant here). ``threshold_sec_per_km`` (from the current PlanState's
    athlete_baseline) anchors unmatched-run intensity classification to the
    athlete's own threshold; without it unmatched runs stay unclassified at the
    easy floor.
    """
    active_fetch = fetch if fetch is not None else _default_fetch
    activities, activities_malformed_rows = _fetch_activities(credentials, window, fetch=active_fetch)
    wellness, wellness_malformed_rows = _fetch_wellness(credentials, window, fetch=active_fetch)

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
    # wellness history (issue #111): a non-dict row inside an otherwise-valid list is
    # still dropped, exactly as before, but the drop is now counted here so a broad
    # schema change is visible in `unknowns` instead of looking identical to "nothing to
    # report".
    if activities_malformed_rows:
        notes.append(f"intervals_activities_malformed_rows:{activities_malformed_rows}")
    if wellness_malformed_rows:
        notes.append(f"intervals_wellness_malformed_rows:{wellness_malformed_rows}")
    recent_actuals = _build_recent_actuals(activities, window, notes, threshold_sec_per_km)
    segment_execution = _build_segment_execution(
        activities, window, credentials, notes, fetch=active_fetch
    )
    # One more request, same credential, same read-only GET: the Run sport settings'
    # own max HR, so a later divergence check has both sides to compare (see
    # context_core._max_hr_divergence_note). Never blocks the build -- see
    # _fetch_run_sport_settings for why every failure degrades to None instead.
    sport_settings_max_hr = _run_sport_settings_max_hr(credentials, fetch=active_fetch)

    return SourceDomain(
        sources=[source_entry],
        freshness_activities="fresh",
        freshness_recovery=_recovery_freshness(wellness, window),
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
        sport_settings_max_hr=sport_settings_max_hr,
        extra_unknowns=list(notes),
    )
