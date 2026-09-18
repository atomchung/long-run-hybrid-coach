"""Everything this service reads from its environment, read once and in one place.

Two things are deliberately not configurable. The model is pinned in ``model.py``: a demo
that can be pointed at a different model by an environment variable is a demo that can
silently stop being the thing that was accepted. And the site origin below is compiled in
rather than supplied, so a misconfigured deployment fails closed on CORS instead of
opening to everybody.

Everything else is a limit, and every limit has a default that is safe for an anonymous
public endpoint. Raising one is a deployment decision; none of them can be raised from a
request.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field


API_KEY_ENV = "OPENAI_API_KEY"

# The site the demo is embedded in. Compiled in, not configured -- see the module note.
SITE_ORIGIN = "https://paceandstaystrong.com"

# Where this service answers in production, and the two routes it answers on. Written
# down here because three other things have to agree with it -- the site's `data-endpoint`,
# this repository's own documentation, and the Railway custom domain -- and a constant is
# the only version of that agreement a test can check.
#
# Its own host, never the gateway's. `mcp.paceandstaystrong.com` serves connected athletes
# and their OAuth; putting anonymous demo traffic on it would put a public playground
# inside the production failure domain and behind the reviewed MCP surface.
PUBLIC_HOST = "demo-api.paceandstaystrong.com"

RESPOND_PATH = "/demo/v1/respond"
HEALTH_PATH = "/healthz"

PUBLIC_ENDPOINT = f"https://{PUBLIC_HOST}{RESPOND_PATH}"

# The local fallback. Railway injects PORT and expects the process to bind it; this is
# only what a developer gets when nothing does.
DEFAULT_PORT = 8433

# Railway's own variable, read first. A service that ignores it binds a port nothing is
# routed to, and the platform's health check fails on a process that is running perfectly.
PLATFORM_PORT_ENV = "PORT"


class ConfigError(RuntimeError):
    """A deployment that cannot be served as configured."""


def _int(env: dict[str, str], name: str, default: int, *, minimum: int = 1) -> int:
    raw = env.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        value = int(raw)
    except ValueError as error:
        raise ConfigError(f"{name} must be an integer") from error
    if value < minimum:
        raise ConfigError(f"{name} must be >= {minimum}")
    return value


def _port(env: dict[str, str]) -> int:
    """Railway's ``PORT`` first, then ``COACH_DEMO_PORT``, then the local fallback.

    The platform assigns the port and routes to it; a service that binds its own number
    instead is unreachable, and the health check fails against a process that is otherwise
    working. ``COACH_DEMO_PORT`` stays for a host that injects nothing, and is deliberately
    second so it cannot shadow the platform on a deployment that sets both.
    """
    if (env.get(PLATFORM_PORT_ENV) or "").strip():
        return _int(env, PLATFORM_PORT_ENV, DEFAULT_PORT)
    return _int(env, "COACH_DEMO_PORT", DEFAULT_PORT)


def _origins(raw: str | None) -> tuple[str, ...]:
    """The site origin plus whatever preview origins the deployment names.

    Exact-match only, and https-only apart from a loopback origin, which is what a
    developer serving the page locally needs and what nothing on the public internet can
    present. A wildcard is not accepted in any position: this endpoint answers with a
    model's words about an athlete, and `*` on it is an open relay with a bill attached.
    """
    origins = [SITE_ORIGIN]
    for candidate in (raw or "").split(","):
        origin = candidate.strip()
        if not origin:
            continue
        if "*" in origin:
            raise ConfigError("COACH_DEMO_ALLOWED_ORIGINS does not accept a wildcard")
        loopback = origin.startswith("http://localhost:") or origin.startswith(
            "http://127.0.0.1:"
        )
        if not origin.startswith("https://") and not loopback:
            raise ConfigError(f"origin must be https (or loopback http): {origin!r}")
        if origin not in origins:
            origins.append(origin)
    return tuple(origins)


@dataclass(frozen=True)
class DemoConfig:
    """One immutable snapshot of how this process is configured to behave."""

    host: str = "0.0.0.0"
    port: int = DEFAULT_PORT
    allowed_origins: tuple[str, ...] = (SITE_ORIGIN,)

    # A session is a conversation about a fixture, not an account. An hour, because what
    # this bounds is a visitor reading: a turn takes ten to thirty seconds, the answer is a
    # week of training, and at fifteen minutes a conversation expired while it was still
    # being read. An expired id does not resume -- it comes back as turn one with no
    # history, which is indistinguishable from a coach that forgot the last answer. Idle
    # time only: the store still holds 500 conversations, and a deploy still starts every
    # one of them over.
    session_ttl_seconds: int = 3600
    max_sessions: int = 500
    max_turns_per_session: int = 12
    max_message_chars: int = 1200
    max_body_bytes: int = 8 * 1024

    # Per-IP first, then a ceiling for the whole process. The second is what remains when
    # the first is defeated by a rotated forwarded-for header, and it is also the bill.
    requests_per_minute_per_client: int = 12
    requests_per_minute_global: int = 120

    # Railway terminates TLS in front of this process, so the socket peer is its proxy and
    # every visitor would share one bucket. "forwarded" reads the right-most entry of
    # X-Forwarded-For -- the one the proxy in front appended, rather than anything the
    # caller put there -- so set "peer" when nothing fronts the service.
    client_ip_source: str = "forwarded"

    model_timeout_seconds: int = 60
    model_base_url: str = "https://api.openai.com/v1"
    api_key: str | None = None

    unknown_env: tuple[str, ...] = field(default=())

    @property
    def has_api_key(self) -> bool:
        return bool(self.api_key)


_PREFIX = "COACH_DEMO_"
_KNOWN_ENV = frozenset(
    {
        "COACH_DEMO_HOST",
        "COACH_DEMO_PORT",
        "COACH_DEMO_ALLOWED_ORIGINS",
        "COACH_DEMO_SESSION_TTL_SECONDS",
        "COACH_DEMO_MAX_SESSIONS",
        "COACH_DEMO_MAX_TURNS",
        "COACH_DEMO_MAX_MESSAGE_CHARS",
        "COACH_DEMO_MAX_BODY_BYTES",
        "COACH_DEMO_RATE_PER_MINUTE",
        "COACH_DEMO_GLOBAL_RATE_PER_MINUTE",
        "COACH_DEMO_CLIENT_IP_SOURCE",
        "COACH_DEMO_MODEL_TIMEOUT_SECONDS",
        "COACH_DEMO_MODEL_BASE_URL",
    }
)


def from_environment(environ: dict[str, str] | None = None) -> DemoConfig:
    """Read the configuration, refusing a value this service cannot honour.

    A missing key is *not* refused here. Startup wants to say so in its own words, and a
    request wants to answer with a status code rather than a stack trace, so both are
    handled by their own layer and this only reports whether one is present.
    """
    env = dict(os.environ if environ is None else environ)
    ip_source = env.get("COACH_DEMO_CLIENT_IP_SOURCE", "forwarded").strip() or "forwarded"
    if ip_source not in {"forwarded", "peer"}:
        raise ConfigError("COACH_DEMO_CLIENT_IP_SOURCE must be 'forwarded' or 'peer'")
    base_url = (env.get("COACH_DEMO_MODEL_BASE_URL") or "").strip()
    if base_url and not base_url.startswith("https://"):
        raise ConfigError("COACH_DEMO_MODEL_BASE_URL must be https")
    key = (env.get(API_KEY_ENV) or "").strip()
    return DemoConfig(
        host=env.get("COACH_DEMO_HOST", "0.0.0.0").strip() or "0.0.0.0",
        port=_port(env),
        allowed_origins=_origins(env.get("COACH_DEMO_ALLOWED_ORIGINS")),
        session_ttl_seconds=_int(env, "COACH_DEMO_SESSION_TTL_SECONDS", 3600, minimum=30),
        max_sessions=_int(env, "COACH_DEMO_MAX_SESSIONS", 500),
        max_turns_per_session=_int(env, "COACH_DEMO_MAX_TURNS", 12),
        max_message_chars=_int(env, "COACH_DEMO_MAX_MESSAGE_CHARS", 1200),
        max_body_bytes=_int(env, "COACH_DEMO_MAX_BODY_BYTES", 8 * 1024, minimum=256),
        requests_per_minute_per_client=_int(env, "COACH_DEMO_RATE_PER_MINUTE", 12),
        requests_per_minute_global=_int(env, "COACH_DEMO_GLOBAL_RATE_PER_MINUTE", 120),
        client_ip_source=ip_source,
        model_timeout_seconds=_int(env, "COACH_DEMO_MODEL_TIMEOUT_SECONDS", 60),
        model_base_url=base_url or "https://api.openai.com/v1",
        api_key=key or None,
        unknown_env=tuple(
            sorted(
                name for name in env
                if name.startswith(_PREFIX) and name not in _KNOWN_ENV
            )
        ),
    )
