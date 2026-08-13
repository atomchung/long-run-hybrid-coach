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
    _coverage_entry,
    _median_trend,
    _safe_float,
)
from .store import REPO_ROOT


BASE_URL = "https://intervals.icu/api/v1/athlete/{athlete_id}"
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
                raise ContextBuildError(f"intervals.icu request failed with HTTP {exc.code}") from exc
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


# --------------------------------------------------------------------------------------
# Endpoint readers
#
# Field names below were verified with live read-only GETs against the real account on
# 2026-08-10 (see /activities and /wellness response samples during development). They
# are hardcoded rather than rediscovered per request, matching the health.db mapping
# convention in source_personal_os.py.
# --------------------------------------------------------------------------------------


def _fetch_activities(
    credentials: IntervalsCredentials,
    window: BuildWindow,
    *,
    fetch: Fetcher,
) -> list[dict[str, Any]]:
    """The 42-day cycle-planning activity window. Confirmed fields: id, type,
    start_date_local, moving_time (s), distance (m), average_speed (m/s),
    average_heartrate, paired_event_id,
    total_elevation_gain (m), and feel (1-5 athlete self-rating)."""
    query = f"/activities?oldest={window.window42_start.isoformat()}&newest={window.window42_end.isoformat()}"
    payload = _get_json(query, credentials, fetch=fetch)
    return [row for row in payload if isinstance(row, dict)] if isinstance(payload, list) else []


def _fetch_wellness(
    credentials: IntervalsCredentials,
    window: BuildWindow,
    *,
    fetch: Fetcher,
) -> list[dict[str, Any]]:
    """The 7-day coverage/trends window. Confirmed fields: id (the date, e.g.
    "2026-08-09"), sleepSecs, sleepScore, hrv, restingHR. This account's Garmin health
    feed is effectively not flowing yet (confirmed live: sleepSecs/sleepScore/hrv were
    null on both rows returned; restingHR was present on only one of two) -- callers must
    treat missing fields as genuinely missing, never fabricate them."""
    query = f"/wellness?oldest={window.window_start.isoformat()}&newest={window.window_end.isoformat()}"
    payload = _get_json(query, credentials, fetch=fetch)
    return [row for row in payload if isinstance(row, dict)] if isinstance(payload, list) else []


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


def _map_activity_sport(activity_type: Any) -> str | None:
    """Run*->"running", WeightTraining/strength->"strength", everything else skipped.

    Hardcoded against the verified intervals.icu vocabulary: the live account's one
    sample activity had ``type: "WeightTraining"``. A running type was not observed live
    (this account has no run in its history yet), so the "Run*" prefix match follows the
    task's documented convention and intervals.icu/Strava's public type vocabulary
    (e.g. "Run", "TrailRun") rather than a second live sample.
    """
    lowered = str(activity_type or "").lower()
    if lowered.startswith("run"):
        return "running"
    if lowered == "weighttraining" or "strength" in lowered:
        return "strength"
    return None


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
    for row in activities:
        day = _activity_date(row)
        if day is None or not (window.window42_start <= day <= window.window42_end):
            continue
        sport = _map_activity_sport(row.get("type"))
        if sport is None:
            continue
        raw_id = row.get("id")
        if not raw_id:
            continue
        activity_id = f"intervals:{raw_id}"
        if sport == "strength":
            adaptation, body_stress, cost = "strength", "full", "moderate"
        else:
            adaptation, cost = _classify_running(
                _safe_float(row.get("average_speed")), activity_id, notes, threshold_sec_per_km
            )
            body_stress = "lower"
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
                "completion": "completed",
                "elevation_gain_m": _safe_float(row.get("total_elevation_gain")),
                "subjective_feel": _safe_feel(row.get("feel")),
            }
        )
    # Keep the bounded 42-day read intact. Cycle planning needs the full window; a
    # top-20 cap silently drops running evidence for athletes who lift most days.
    candidates.sort(key=lambda item: (item["date"], item["activity_id"]), reverse=True)
    recent = candidates
    recent.sort(key=lambda item: (item["date"], item["activity_id"]))
    return recent


_RECOVERY_FIELDS = ("sleepScore", "hrv", "restingHR")


def _recovery_freshness(wellness: list[dict[str, Any]], window: BuildWindow) -> str:
    """Grade the recovery feed by observed signal values, not by HTTP success.

    The wellness endpoint returns a row for a day even when nothing was measured (every
    field null), so a successful GET -- and even a recent row date -- proves nothing
    about recovery evidence. What a daily decision actually needs is the multi-signal
    picture this repo's whole recovery policy is built on (one noisy metric never flips
    a plan), so freshness counts recovery *signals*, per field, by the newest date each
    field carries a real value inside the 7-day window:

      - no field has any value        -> "failed"  (the feed is effectively silent)
      - >=2 fields fresh (<=1 day)    -> "fresh"   (a multi-signal read is possible)
      - >=2 fields have values, older -> "stale"   (signals exist but are not current)
      - exactly 1 field has values    -> "partial" (a single signal is never enough)

    Per-day counts stay in coverage and per-signal direction stays in recovery_trends;
    this field only answers "may a normal daily decision lean on recovery at all".
    """
    domain_latest: dict[str, dt.date] = {}
    for row in wellness:
        day = _wellness_date(row)
        if day is None or not (window.window_start <= day <= window.window_end):
            continue
        for field in _RECOVERY_FIELDS:
            value = _safe_float(row.get(field))
            # 0 is a sentinel, not a measurement -- no living athlete has a resting
            # HR, HRV, or sleep score of zero, so 0 must never count as evidence.
            if value is not None and value > 0:
                current = domain_latest.get(field)
                if current is None or day > current:
                    domain_latest[field] = day
    if not domain_latest:
        return "failed"
    fresh_fields = sum(
        1 for latest in domain_latest.values() if (window.window_end - latest).days <= 1
    )
    if fresh_fields >= 2:
        return "fresh"
    if len(domain_latest) >= 2:
        return "stale"
    return "partial"


def _build_recovery_domain(
    wellness: list[dict[str, Any]],
    window: BuildWindow,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any]]:
    """Return (coverage_sleep, coverage_hrv, coverage_resting_hr, recovery_trends).

    Sleep uses sleepScore (same +/-10-point median logic as the personal-os sleep-percent
    trend). HRV uses +/-10% of the window median -- there is no Garmin baseline JSON on
    this path, unlike personal-os's HRV trend. Resting HR shares the same median logic
    as sleep.
    """
    sleep_values: dict[dt.date, float] = {}
    hrv_values: dict[dt.date, float] = {}
    resting_values: dict[dt.date, float] = {}
    for row in wellness:
        day = _wellness_date(row)
        if day is None or not (window.window_start <= day <= window.window_end):
            continue
        # Same rule as _recovery_freshness: 0 is a sentinel, not a measurement. Letting
        # it into coverage/trends while freshness excludes it would have the context
        # calling the feed failed and the trend within_baseline at once.
        sleep_score = _safe_float(row.get("sleepScore"))
        if sleep_score is not None and sleep_score > 0:
            sleep_values[day] = sleep_score
        hrv = _safe_float(row.get("hrv"))
        if hrv is not None and hrv > 0:
            hrv_values[day] = hrv
        resting_hr = _safe_float(row.get("restingHR"))
        if resting_hr is not None and resting_hr > 0:
            resting_values[day] = resting_hr

    recovery_trends = {
        "sleep": _median_trend(sleep_values, window.window_end, band_points=10.0),
        "hrv": _median_trend(hrv_values, window.window_end, band_fraction=0.10),
        "resting_hr": _median_trend(resting_values, window.window_end, band_points=10.0),
    }
    return (
        _coverage_entry(len(sleep_values)),
        _coverage_entry(len(hrv_values)),
        _coverage_entry(len(resting_values)),
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
    activities = _fetch_activities(credentials, window, fetch=active_fetch)
    wellness = _fetch_wellness(credentials, window, fetch=active_fetch)

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

    coverage_activities = _coverage_entry(len(_activity_coverage_days(activities, window)))
    coverage_sleep, coverage_hrv, coverage_resting_hr, recovery_trends = _build_recovery_domain(wellness, window)

    notes: list[str] = []
    recent_actuals = _build_recent_actuals(activities, window, notes, threshold_sec_per_km)

    return SourceDomain(
        sources=[source_entry],
        freshness_activities="fresh",
        freshness_recovery=_recovery_freshness(wellness, window),
        # _build_recent_actuals reads the whole 42-day window and caps nothing, so every
        # session of a cycle was searched for an attachment.
        actuals_window_start=window.window42_start,
        coverage_activities=coverage_activities,
        coverage_sleep=coverage_sleep,
        coverage_hrv=coverage_hrv,
        coverage_resting_hr=coverage_resting_hr,
        recovery_trends=recovery_trends,
        recent_actuals=recent_actuals,
        extra_unknowns=list(notes),
    )
