"""Short-lived confirmation proposals that carry their own binding.

A confirmation only means something if the thing confirmed cannot change underneath it.
The delivery boundary gets that from a content hash over a proposal the client hands back
whole. A plan change cannot: after the agent-facing contract became a small change request
(``plan_change``), the candidate PlanState and DecisionEvent never leave the server, so
there is nothing for the client to hand back and hash.

So a proposal here is a signed statement *about* the material instead: who it was issued
to, which evidence and plan version it was computed from, what it projected to, and how
long it is good for. The server re-derives the same projection at apply time and refuses
unless every claim still matches.

Two properties this keeps, deliberately:

- **Nothing is remembered between requests.** There is still no proposal database, so a
  restarted process cannot forget an approval, and an approval cannot outlive its claims.
- **The client cannot author one.** The signature is keyed with the server's own secret,
  which is what makes the lifetime real: a plain hash over public material could simply be
  recomputed with a longer expiry.

The payload is signed, not encrypted, and therefore holds no secret and no identifier the
holder does not already have: an owner is bound through ``binding`` -- a keyed one-way
handle -- rather than by writing the owner id into something the client can read.
"""

from __future__ import annotations

import base64
import datetime as dt
import hashlib
import hmac
import json
from typing import Any


# How long a proposal is good for where a clock is still the answer. It used to be the
# whole answer, documented as one conversation turn's worth of confirmation -- which is a
# synchronous assumption this product does not meet. Authoring a week takes the coach
# minutes, and an athlete answers when they next pick up their phone, so a confirmation
# arriving twenty minutes later is normal use rather than a stale request (issue #358).
#
# What it bounds now is narrower, and each caller says which case it is in:
#
#   - an erasure, which cannot be taken back and whose preview is about what disappears;
#   - a first plan, the other write no later call can undo;
#   - a plan change whose evidence could not be re-read at all -- a provider outage, a
#     refused credential -- where the comparison that replaced this clock cannot be made.
#
# A plan change whose evidence *can* be read is not bound by it in either direction: it
# commits however long it took when nothing moved, and is previewed again when something
# did, however recent it is.
PROPOSAL_TTL_SECONDS = 900

# Domain separation. The same key fingerprints access tokens elsewhere; prefixing the
# signed message keeps a value from one use from ever validating as the other.
_SIGNATURE_DOMAIN = b"garmin-coach-loop/proposal/v1"
_BINDING_DOMAIN = "garmin-coach-loop/binding/v1:"


class ProposalError(RuntimeError):
    """A proposal could not be opened.

    ``expired`` separates the two answers a caller reports differently: material that was
    never issued by this gateway, and material that was but is no longer current.
    """

    def __init__(self, message: str, *, expired: bool = False):
        super().__init__(message)
        self.expired = expired


def binding(value: str, *, key: bytes) -> str:
    """A keyed one-way handle for a value that must be bound but never disclosed."""
    if not isinstance(value, str) or not value:
        raise ProposalError("a binding needs a non-empty value")
    if not isinstance(key, (bytes, bytearray)) or not bytes(key):
        raise ProposalError("a proposal key must be non-empty bytes")
    message = (_BINDING_DOMAIN + value).encode("utf-8")
    return hmac.new(bytes(key), message, hashlib.sha256).hexdigest()


def _encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _signature(payload: bytes, key: bytes) -> bytes:
    return hmac.new(bytes(key), _SIGNATURE_DOMAIN + b"." + payload, hashlib.sha256).digest()


def _utc_iso(moment: dt.datetime) -> str:
    return (
        moment.astimezone(dt.timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def issue_proposal(
    claims: dict[str, Any],
    *,
    key: bytes,
    now: dt.datetime,
    ttl_seconds: int = PROPOSAL_TTL_SECONDS,
) -> dict[str, Any]:
    """Sign one set of claims, stamped with the lifetime it is good for.

    ``issued_at`` and ``expires_at`` are written here rather than accepted from the
    caller: a lifetime the requester chooses is not a lifetime.
    """
    if not isinstance(claims, dict) or not claims:
        raise ProposalError("a proposal needs claims to bind")
    if "issued_at" in claims or "expires_at" in claims:
        raise ProposalError("a proposal stamps its own lifetime")
    issued = now.astimezone(dt.timezone.utc).replace(microsecond=0)
    body = {
        **claims,
        "issued_at": _utc_iso(issued),
        "expires_at": _utc_iso(issued + dt.timedelta(seconds=ttl_seconds)),
    }
    payload = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    encoded = payload.encode("utf-8")
    return {
        "proposal": f"{_encode(encoded)}.{_encode(_signature(encoded, key))}",
        "claims": body,
        "issued_at": body["issued_at"],
        "expires_at": body["expires_at"],
    }


def open_proposal(proposal: Any, *, key: bytes, now: dt.datetime) -> dict[str, Any]:
    """Verify one proposal and return its claims plus whether its lifetime has run out.

    Expiry is reported rather than raised: whether a stale confirmation is refused or
    read as an already-finished write is the caller's decision, and only the caller can
    see whether anything would actually be written.
    """
    if not isinstance(proposal, str) or proposal.count(".") != 1:
        raise ProposalError("proposal is not a value this gateway issued")
    encoded_payload, encoded_signature = proposal.split(".")
    try:
        payload = _decode(encoded_payload)
        signature = _decode(encoded_signature)
    except (ValueError, TypeError) as exc:
        raise ProposalError("proposal is not a value this gateway issued") from exc
    if not hmac.compare_digest(_signature(payload, key), signature):
        raise ProposalError("proposal is not a value this gateway issued")
    try:
        claims = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProposalError("proposal is not a value this gateway issued") from exc
    if not isinstance(claims, dict):
        raise ProposalError("proposal is not a value this gateway issued")
    expires_at = claims.get("expires_at")
    try:
        deadline = dt.datetime.fromisoformat(str(expires_at).replace("Z", "+00:00"))
    except ValueError as exc:
        raise ProposalError("proposal is not a value this gateway issued") from exc
    if deadline.tzinfo is None:
        raise ProposalError("proposal is not a value this gateway issued")
    return {
        "claims": claims,
        "expired": now.astimezone(dt.timezone.utc) >= deadline,
    }
