"""Structured security events for the OAuth and MCP trust boundary.

Prevention is never complete, so the service has to be able to answer, afterwards, what
happened at its own boundary: whether an untrusted client tried to register, how far one
authorization got, which step refused it. Without that, an incident report is answered
with a shrug.

The whole design constraint is that this evidence must not itself become the leak. A
security log of an authorization server is exactly where a credential ends up when nobody
decides in advance what may be written -- so this module decides in advance, structurally
rather than by discipline:

- **A fixed field set.** ``emit`` builds six keys and takes no free-form payload. There
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
- **Protocol dates, never raw headers.** A refused revision is a canonical ASCII date,
  or the fixed label ``invalid`` or ``duplicate``. No other header content is retained.

What is deliberately *not* here: no second store, no alerting, no rate limiter, no
retention of its own. These events go to the same stream everything else in the process
goes to (see ``docs/ops/security-events.md`` for where that is and how long it lives).
The moment real traffic shows that stream is not enough to investigate with, the answer
is a longer-lived sink chosen against that evidence -- not a framework added ahead of it.
"""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import logging
import re
import urllib.parse
from datetime import date
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
MCP_PROTOCOL = "mcp_protocol"

EVENTS: frozenset[str] = frozenset(
    {
        CLIENT_REGISTRATION,
        AUTHORIZATION,
        PROVIDER_CALLBACK,
        TOKEN_ISSUANCE,
        MCP_AUTHENTICATION,
        MCP_PROTOCOL,
    }
)

ACCEPTED = "accepted"
REFUSED = "refused"
RESULTS: frozenset[str] = frozenset({ACCEPTED, REFUSED})

# Why something was refused, in the vocabulary of the boundary rather than of the code:
# each of these is a distinct thing an operator would want to count or search for.
# Historical reason from releases before 1.4.1; never emitted by current admission.
UNTRUSTED_REDIRECT_ORIGIN = "untrusted_redirect_origin"
# The one origin state that refuses. Configured by an operator against concrete abuse or
# compromise evidence, and checked at every authorization rather than at registration
# alone, so it stops the client ids already issued on that origin too.
BLOCKED_REDIRECT_ORIGIN = "blocked_redirect_origin"
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
CODE_ALREADY_REDEEMED = "code_already_redeemed"
CLIENT_MISMATCH = "client_mismatch"
PKCE_VERIFICATION_FAILED = "pkce_verification_failed"
REDIRECT_MISMATCH = "redirect_mismatch"
RESOURCE_MISMATCH = "resource_mismatch"
MISSING_BEARER = "missing_bearer"
UNRECOGNIZED_TOKEN = "unrecognized_token"
AUDIENCE_MISMATCH = "audience_mismatch"
UNKNOWN_OWNER = "unknown_owner"
UNSUPPORTED_PROTOCOL_VERSION = "unsupported_protocol_version"
UNCLASSIFIED = "unclassified"

REASONS: frozenset[str] = frozenset(
    {
        UNTRUSTED_REDIRECT_ORIGIN,
        BLOCKED_REDIRECT_ORIGIN,
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
        CODE_ALREADY_REDEEMED,
        CLIENT_MISMATCH,
        PKCE_VERIFICATION_FAILED,
        REDIRECT_MISMATCH,
        RESOURCE_MISMATCH,
        MISSING_BEARER,
        UNRECOGNIZED_TOKEN,
        AUDIENCE_MISMATCH,
        UNKNOWN_OWNER,
        UNSUPPORTED_PROTOCOL_VERSION,
        UNCLASSIFIED,
    }
)

# Every key an event may ever carry. The test that holds this module's privacy property
# reads this tuple, so a field added without a decision fails that test rather than
# reaching production.
FIELDS: tuple[str, ...] = (
    "event", "result", "reason", "origin", "client", "protocol_version",
)

_FINGERPRINT_LABEL = b"garmin-coach-loop/security-event/client/v1"
# Long enough that two clients on one deployment will not collide, short enough that the
# value reads as the opaque handle it is rather than as something to decode.
_FINGERPRINT_CHARACTERS = 16

# A host with an optional port, or a bracketed IPv6 literal -- the same shape the gateway
# accepts as its own public host. Kept here too so this module can refuse an origin on its
# own, without importing the HTTP layer that calls it.
#
# `[0-9]` rather than `\d` on the port: `\d` matches every decimal digit Unicode defines,
# and a port written in another script is a port a person cannot compare to the one an
# operator configured.
_ORIGIN_HOST = re.compile(r"^(?:[A-Za-z0-9._~-]+|\[[0-9A-Fa-f:.]+\])(?::[0-9]{1,5})?$")
_PROTOCOL_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")


def protocol_version(raw_values: Any) -> str | None:
    """Reduce all occurrences of one revision header to bounded diagnostic evidence.

    ``get_all`` preserves duplicate fields that ``get`` hides. A repeated field or a
    comma-joined value is classified without retaining any member of it. A single
    value survives only when it is exactly an ASCII calendar date; even whitespace,
    Unicode lookalikes and impossible dates are invalid. This never decides whether
    the request is accepted -- the transport's existing check owns that decision.
    """
    if raw_values is None:
        return None
    if not isinstance(raw_values, (list, tuple)):
        return "invalid"
    if not raw_values:
        return None
    if len(raw_values) > 1:
        return "duplicate"
    raw = raw_values[0]
    if not isinstance(raw, str):
        return "invalid"
    if "," in raw:
        return "duplicate"
    if len(raw) != 10 or _PROTOCOL_DATE.fullmatch(raw) is None:
        return "invalid"
    try:
        date.fromisoformat(raw)
    except ValueError:
        return "invalid"
    return raw


# The port a scheme reaches when a URL states none, so that stating it is not a second
# origin. RFC 6454 defines an origin partly by the port; a browser and DNS do not care
# which of the two spellings a URL used.
_DEFAULT_PORTS = {"http": "80", "https": "443"}


def _browser_ipv6(address: str) -> str:
    # ipaddress validates the address, but its string format changed in Python 3.11.16
    # for mapped IPv4. WHATWG serializes eight hex pieces, compressing the first longest
    # zero run. Format the validated 128 bits explicitly, never its version-dependent str.
    value = int(ipaddress.IPv6Address(address))
    pieces = [f"{(value >> shift) & 0xffff:x}" for shift in range(112, -1, -16)]
    start, length = 0, 0
    for index in range(8):
        end = index
        while end < 8 and pieces[end] == "0":
            end += 1
        if end - index > length:
            start, length = index, end - index
    if length < 2:
        return ":".join(pieces)
    return ":".join(pieces[:start]) + "::" + ":".join(pieces[start + length:])


def normalized_authority(scheme: str, netloc: str) -> str | None:
    """One origin in the single spelling two of them can be compared in, or ``None``.

    The displayed origin must equal the browser's destination origin. Accept canonical
    IPv4 only; reject numeric final labels that WHATWG could parse as alternate IPv4.
    IPv6 is validated and compressed, and default ports and case are normalized.
    Trailing dots are refused: DNS equivalence is not browser origin equivalence.
    Userinfo, escapes, Unicode authorities, invalid IDNA and ambiguous ports fail closed.
    """
    host = netloc.lower()
    if not _ORIGIN_HOST.fullmatch(host):
        return None
    if host.startswith("["):
        address, _, remainder = host.partition("]")
        try:
            address = "[" + _browser_ipv6(address[1:]) + "]"
        except ValueError:
            return None
        port = remainder[1:] if remainder.startswith(":") else remainder
    else:
        address, _, port = host.partition(":")
        labels = address.split(".")
        if not all(labels):
            return None
        # WHATWG's "ends in a number" branch also catches hex and shortened IPv4.
        # Do not implement its permissive parser; only dotted decimal survives.
        last = labels[-1]
        if last.isdigit() or re.fullmatch(r"0x[0-9a-f]*", last):
            try:
                if str(ipaddress.IPv4Address(address)) != address:
                    return None
            except ValueError:
                return None
        for label in labels:
            if label.startswith("xn--"):
                try:
                    if label.encode("ascii").decode("idna").encode("idna").decode("ascii") != label:
                        return None
                except UnicodeError:
                    return None
    if port:
        # One spelling per port, and only ports that exist. A browser reads `:0443` as
        # `:443` and refuses `:99999` outright, so accepting either would put a string on
        # the security log that is not where the browser goes -- and would let an origin
        # an operator blocked come back by typing a zero.
        if port != str(int(port)) or int(port) > 65535:
            return None
        if port == _DEFAULT_PORTS.get(scheme):
            return f"{scheme}://{address}"
        return f"{scheme}://{address}:{port}"
    return f"{scheme}://{address}"


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
    return normalized_authority(parts.scheme.lower(), parts.netloc)


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
    protocol_versions: Any = None,
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
            "protocol_version": protocol_version(protocol_versions),
        }
        LOGGER.info("security %s", json.dumps(payload, sort_keys=True))
    except Exception:  # pragma: no cover - defensive; evidence never breaks the boundary
        # Deliberately not another log call: the reason this one is here at all is that
        # logging failed, and a second attempt would be the same failure inside `except`.
        pass
