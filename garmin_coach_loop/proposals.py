"""Signed confirmation bindings and the internal prepared-record integrity primitive.

Tokens bind owner, kind, evidence and exact effects. Their claims are signed, not
encrypted: private effect/context/recovery bytes stay in the gateway's bounded memory,
while the owner is represented by a keyed handle. No proposal database is introduced.

Authentication reports expiry rather than refusing it. The caller owns per-kind expiry
and checks durable receipts before requiring an ephemeral record. Current public routes
still use their existing projection path until the separate catalogue migration.
"""

from __future__ import annotations

import base64
import copy
import datetime as dt
import hashlib
import hmac
import json
from typing import Any

from .store import canonical_hash


# How long a proposal is good for where a clock is still the answer. It used to be the
# whole answer, documented as one conversation turn's worth of confirmation -- which is a
# synchronous assumption this product does not meet. Authoring a week takes the coach
# minutes, and an athlete answers when they next pick up their phone, so a confirmation
# arriving twenty minutes later is normal use rather than a stale request (issue #358).
#
# What it bounds now is narrower, and each caller says which case it is in:
#
#   - a first plan, the one write no later call can undo;
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


def prepare_transaction_record(
    claims: dict[str, Any], *, effect: dict[str, Any], preview: Any,
    validation: dict[str, Any], recovery_inputs: dict[str, Any],
    confirmation_required: bool,
) -> dict[str, Any]:
    """Freeze one server-authored transaction for the existing bounded hold cache.

    Integrity boundary only: swapping an effect, safety context or recovery request
    after preview would spend a yes on different material. A warning cannot make that
    write authorized. Hash equality permits every coaching decision and unknown signal;
    structural/safety validation and current-state checks remain the writers' job.
    The false-positive cost is reprepare if retained material is corrupted, never rest.

    ``claims`` comes from the existing kind-specific builders. Validation contains the
    bound context for a decision, or the first-plan red flags and preview-local day.
    Recovery inputs can author a replacement preview only, never the committed effect.
    This function neither signs, persists, expires nor commits anything.
    """
    material = copy.deepcopy({
        "effect": effect, "preview": preview, "validation": validation,
        "recovery_inputs": recovery_inputs,
    })
    bound = copy.deepcopy(claims)
    kind = bound.get("kind")
    if kind not in {"initialization", "decision", "delivery", "withdrawal"}:
        raise ProposalError("unsupported prepared transaction kind")
    if any(not isinstance(bound.get(name), str) or not bound[name] for name in ("owner", "release")):
        raise ProposalError("a prepared transaction requires owner and release bindings")
    if any(name in bound for name in ("issued_at", "expires_at", "record_hash")):
        raise ProposalError("a prepared transaction cannot supply its lifetime or record hash")
    if not isinstance(confirmation_required, bool):
        raise ProposalError("confirmation_required must be a boolean")
    effect = material["effect"]
    validation = material["validation"]
    if not all(isinstance(material[name], dict) for name in ("effect", "validation", "recovery_inputs")):
        raise ProposalError("prepared effect, validation and recovery inputs must be objects")
    derived = {
        "effect_hash": canonical_hash(effect),
        "preview_hash": canonical_hash(material["preview"]),
        "validation_hash": canonical_hash(validation),
        "recovery_hash": canonical_hash(material["recovery_inputs"]),
        "confirmation_required": confirmation_required,
    }
    try:
        if kind == "initialization":
            derived["plan_hash"] = canonical_hash(effect["plan"])
        elif kind == "decision":
            derived.update(
                after_hash=canonical_hash(effect["after_plan"]),
                event_hash=canonical_hash(effect["decision_event"]),
                context_hash=canonical_hash(validation["context"]),
            )
        else:
            # The set's existing hash excludes its own proposal_hash field. Preserve
            # that identity while record_hash binds the complete retained object.
            derived["effect_hash"] = canonical_hash({
                name: value for name, value in effect.items() if name != "proposal_hash"
            })
            if effect.get("proposal_hash") != derived["effect_hash"]:
                raise ProposalError("prepared delivery set differs from its hash")
            derived.update(plan_id=effect["plan_id"], base_version=effect["plan_version"])
        if effect.get("calendar") is not None:
            derived["delivery_hash"] = canonical_hash(effect["calendar"])
        elif "delivery_hash" in bound:
            raise ProposalError("approved calendar effects must be retained with the plan")
        sets = ([effect] if kind in {"delivery", "withdrawal"} else
                [item["set"] for item in (effect.get("calendar") or {}).get("effects", [])])
        derived["side_effect_scope"] = (
            "settings_and_calendar" if any(item.get("settings_changes") for item in sets)
            else "calendar_effects" if any(item.get("items") for item in sets) else "none"
        )
    except KeyError as exc:
        raise ProposalError(f"prepared transaction is missing {exc.args[0]}") from exc
    if "publish_new_workouts" in material["recovery_inputs"]:
        publication = material["recovery_inputs"]["publish_new_workouts"]
        if not isinstance(publication, bool):
            raise ProposalError("recovery publication intent must be a boolean")
        derived["publish_new_workouts"] = publication
    for name, value in derived.items():
        if name in bound and bound[name] != value:
            raise ProposalError(f"prepared transaction differs from its {name} binding")
    bound.update(derived)
    material["bindings"] = bound
    return {"payload": material, "claims": {**copy.deepcopy(bound), "record_hash": canonical_hash(material)}}


def verify_transaction_record(payload: dict[str, Any], *, claims: dict[str, Any]) -> dict[str, Any]:
    """Read an isolated copy against already-authenticated claims, without a TTL gate.

    Callers authenticate owner/kind first and consult durable receipts before requiring
    an ephemeral record. No idempotency or coaching decision is made here.
    """
    material = copy.deepcopy(payload)
    if canonical_hash(material) != claims.get("record_hash"):
        raise ProposalError("prepared transaction record integrity mismatch")
    bound = {name: value for name, value in claims.items()
             if name not in {"record_hash", "issued_at", "expires_at"}}
    if material.get("bindings") != bound:
        raise ProposalError("prepared transaction differs from its signed bindings")
    return material


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
