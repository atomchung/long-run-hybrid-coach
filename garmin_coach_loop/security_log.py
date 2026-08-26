"""Structured security events for the OAuth and MCP trust boundary.

Prevention is never complete, so the service has to be able to answer, afterwards, what
happened at its own boundary: whether an untrusted client tried to register, how far one
authorization got, which step refused it. Without that, an incident report is answered
with a shrug.

The whole design constraint is that this evidence must not itself become the leak. A
security log of an authorization server is exactly where a credential ends up when nobody
decides in advance what may be written -- so this module decides in advance, structurally
rather than by discipline:

- **A fixed field set.** ``emit`` builds five keys and takes no free-form payload. There
  is no ``**extra`` for a later caller to widen, so a token, a code, a body, or an
  athlete identifier has nowhere to go even if somebody tries to pass one.
- **Origins, never URLs.** A callback is written as its normalized origin and nothing
  else -- no path, query, or fragment, which is where a client puts identifiers. A value
  that is not a well-formed origin is written as absent rather than verbatim, so the log
  never carries an unnormalized string somebody else chose.
- **Fingerprints, never identities.** A ``client_id`` is written as a keyed digest of
  itself. Two events from one flow carry the same fingerprint and correlate; the
  fingerprint reveals nothing about the id and does not survive to another deployment,
  whose key differs.
- **Bounded reasons.** A refusal reason is one of the constants below. An unrecognised
  one is written as ``unclassified`` rather than passed through, so a message built from
  request data can never reach the log.

What is deliberately *not* here: no second store, no alerting, no rate limiter, no
retention of its own. These events go to the same stream everything else in the process
goes to (see ``docs/ops/security-events.md`` for where that is and how long it lives).
The moment real traffic shows that stream is not enough to investigate with, the answer
is a longer-lived sink chosen against that evidence -- not a framework added ahead of it.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import re
import urllib.parse
from typing import Any


# Its own logger, not the gateway's: an operator filtering for security events should not
# have to separate them from request lines, and the name is what the filter matches.
LOGGER_NAME = "garmin_coach_loop.security"
LOGGER = logging.getLogger(LOGGER_NAME)

# The boundary crossings worth reconstructing. One per hop of the OAuth chain, plus the
# authenticated use of what that chain issued.
CLIENT_REGISTRATION = "client_registration"
AUTHORIZATION = "authorization"
PROVIDER_CALLBACK = "provider_callback"
TOKEN_ISSUANCE = "token_issuance"
MCP_AUTHENTICATION = "mcp_authentication"

EVENTS: frozenset[str] = frozenset(
    {
        CLIENT_REGISTRATION,
        AUTHORIZATION,
        PROVIDER_CALLBACK,
        TOKEN_ISSUANCE,
        MCP_AUTHENTICATION,
    }
)

ACCEPTED = "accepted"
REFUSED = "refused"
RESULTS: frozenset[str] = frozenset({ACCEPTED, REFUSED})

# Why something was refused, in the vocabulary of the boundary rather than of the code:
# each of these is a distinct thing an operator would want to count or search for.
UNTRUSTED_REDIRECT_ORIGIN = "untrusted_redirect_origin"
INVALID_REDIRECT_URI = "invalid_redirect_uri"
REGISTRATION_TOO_LARGE = "registration_too_large"
UNSUPPORTED_RESPONSE_TYPE = "unsupported_response_type"
UNKNOWN_CLIENT = "unknown_client"
REDIRECT_NOT_REGISTERED = "redirect_not_registered"
MISSING_PKCE_CHALLENGE = "missing_pkce_challenge"
UNKNOWN_AUTHORIZE_STATE = "unknown_authorize_state"
PROVIDER_DENIED = "provider_denied"
PROVIDER_EXCHANGE_FAILED = "provider_exchange_failed"
UNSUPPORTED_GRANT_TYPE = "unsupported_grant_type"
NO_REFRESH_GRANT = "no_refresh_grant"
INVALID_AUTHORIZATION_CODE = "invalid_authorization_code"
CLIENT_MISMATCH = "client_mismatch"
PKCE_VERIFICATION_FAILED = "pkce_verification_failed"
REDIRECT_MISMATCH = "redirect_mismatch"
RESOURCE_MISMATCH = "resource_mismatch"
MISSING_BEARER = "missing_bearer"
UNRECOGNIZED_TOKEN = "unrecognized_token"
AUDIENCE_MISMATCH = "audience_mismatch"
UNKNOWN_OWNER = "unknown_owner"
UNCLASSIFIED = "unclassified"

REASONS: frozenset[str] = frozenset(
    {
        UNTRUSTED_REDIRECT_ORIGIN,
        INVALID_REDIRECT_URI,
        REGISTRATION_TOO_LARGE,
        UNSUPPORTED_RESPONSE_TYPE,
        UNKNOWN_CLIENT,
        REDIRECT_NOT_REGISTERED,
        MISSING_PKCE_CHALLENGE,
        UNKNOWN_AUTHORIZE_STATE,
        PROVIDER_DENIED,
        PROVIDER_EXCHANGE_FAILED,
        UNSUPPORTED_GRANT_TYPE,
        NO_REFRESH_GRANT,
        INVALID_AUTHORIZATION_CODE,
        CLIENT_MISMATCH,
        PKCE_VERIFICATION_FAILED,
        REDIRECT_MISMATCH,
        RESOURCE_MISMATCH,
        MISSING_BEARER,
        UNRECOGNIZED_TOKEN,
        AUDIENCE_MISMATCH,
        UNKNOWN_OWNER,
        UNCLASSIFIED,
    }
)

# Every key an event may ever carry. The test that holds this module's privacy property
# reads this tuple, so a field added without a decision fails that test rather than
# reaching production.
FIELDS: tuple[str, ...] = ("event", "result", "reason", "origin", "client")

_FINGERPRINT_LABEL = b"garmin-coach-loop/security-event/client/v1"
# Long enough that two clients on one deployment will not collide, short enough that the
# value reads as the opaque handle it is rather than as something to decode.
_FINGERPRINT_CHARACTERS = 16

# A host with an optional port, or a bracketed IPv6 literal -- the same shape the gateway
# accepts as its own public host. Kept here too so this module can refuse an origin on its
# own, without importing the HTTP layer that calls it.
_ORIGIN_HOST = re.compile(r"^(?:[A-Za-z0-9._~-]+|\[[0-9A-Fa-f:.]+\])(?::\d{1,5})?$")


def redirect_origin(raw: Any) -> str | None:
    """The scheme-host-port of one callback, or ``None`` when it is not a usable one.

    This is the only part of a redirect URI that may be written down. The path is where a
    client puts a session, an account, or a name; the query is where it puts more of the
    same. Both are dropped here rather than trusted to be uninteresting.
    """
    value = str(raw or "").strip()
    if not value:
        return None
    try:
        parts = urllib.parse.urlsplit(value)
    except ValueError:
        return None
    if parts.scheme.lower() not in {"http", "https"} or parts.username or parts.password:
        return None
    host = parts.netloc.lower()
    if not _ORIGIN_HOST.fullmatch(host):
        return None
    return f"{parts.scheme.lower()}://{host}"


def client_fingerprint(client_id: Any, *, key: bytes) -> str | None:
    """One ``client_id`` as a stable, opaque handle for correlating its own events.

    Keyed, so the value cannot be recomputed from a captured id by anyone without the
    deployment's key, and deterministic, so the registration event and every later event
    of that client carry the same handle across restarts and replicas.
    """
    value = client_id if isinstance(client_id, str) else ""
    if not value:
        return None
    digest = hmac.new(key, _FINGERPRINT_LABEL + value.encode("utf-8"), hashlib.sha256)
    return digest.hexdigest()[:_FINGERPRINT_CHARACTERS]


def emit(
    event: str,
    result: str,
    *,
    key: bytes,
    reason: str | None = None,
    redirect_uri: Any = None,
    client_id: Any = None,
    client_handle: Any = None,
) -> None:
    """Write one security event, or write nothing -- never raise into a request.

    The caller passes what it has: a callback it was given, and either the ``client_id``
    it issued or was presented, or a ``client_handle`` computed from one earlier. Both
    reach the log as the same opaque value -- the second exists because a caller holding
    only the handle should not have to carry the whole id around to log it (see the
    access token in ``gateway.issue_access_token``). ``client_id`` wins if somehow both
    are given, since it is the value the handle is defined by.

    A logging failure is swallowed, because a boundary that refused correctly and then
    failed to record it should still refuse correctly.
    """
    try:
        handle = client_fingerprint(client_id, key=key)
        if handle is None and isinstance(client_handle, str) and client_handle:
            handle = client_handle
        payload = {
            "event": event if event in EVENTS else UNCLASSIFIED,
            "result": result if result in RESULTS else UNCLASSIFIED,
            "reason": reason if reason in REASONS else (None if reason is None else UNCLASSIFIED),
            "origin": redirect_origin(redirect_uri),
            "client": handle,
        }
        LOGGER.info("security %s", json.dumps(payload, sort_keys=True))
    except Exception:  # pragma: no cover - defensive; evidence never breaks the boundary
        # Deliberately not another log call: the reason this one is here at all is that
        # logging failed, and a second attempt would be the same failure inside `except`.
        pass
