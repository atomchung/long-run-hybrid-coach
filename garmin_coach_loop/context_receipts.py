"""Short-lived, owner-scoped snapshots behind the hosted coaching boundary.

The Custom GPT needs to read a CoachContext to make a coaching judgment, but it must not
be responsible for transporting that private, changing-shaped object back to the
gateway.  This module keeps the exact context snapshot outside the append-only
PlanState store and gives the agent a signed, opaque handle for it instead.

Snapshots are private gateway state, not repository fixtures and not durable PlanState.
They are written atomically with mode 0600, expire with the receipt, and are never
logged or returned by this module after the initial session response.  A malformed or
tampered snapshot is an error, never an absent snapshot.
"""

from __future__ import annotations

import copy
import datetime as dt
import json
import os
import re
import secrets
import tempfile
from pathlib import Path
from typing import Any

from .proposals import PROPOSAL_TTL_SECONDS, ProposalError, issue_proposal, open_proposal
from .store import canonical_hash, resolve_state_dir, resolve_state_root


CONTEXT_RECEIPT_SCHEMA_VERSION = "1.0"
CONTEXT_RECEIPT_TTL_SECONDS = PROPOSAL_TTL_SECONDS
CONTEXT_RECEIPTS_DIRECTORY = "context-receipts"
_RECEIPT_ID = re.compile(r"^ctx-[A-Za-z0-9_-]{20,}$")
_SNAPSHOT_FIELDS = {
    "schema_version",
    "receipt_id",
    "owner",
    "plan_id",
    "plan_version",
    "context_id",
    "context_hash",
    "issued_at",
    "expires_at",
    "context",
}


class ContextReceiptError(RuntimeError):
    """A context receipt or its private snapshot cannot be trusted."""


def _utc_iso(moment: dt.datetime) -> str:
    return (
        moment.astimezone(dt.timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _parse_instant(value: Any) -> dt.datetime:
    try:
        parsed = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise ContextReceiptError("context receipt timestamp is invalid") from exc
    if parsed.tzinfo is None:
        raise ContextReceiptError("context receipt timestamp must include a timezone")
    return parsed.astimezone(dt.timezone.utc)


def _owner_receipt_dir(state_root: Path | str, owner_id: str) -> Path:
    root = resolve_state_root(state_root)
    # Reuse the canonical owner/path check.  The returned path is not used: context
    # snapshots deliberately live beside, not inside, the PlanState store.
    resolve_state_dir(owner_id, state_root=root)
    return root / CONTEXT_RECEIPTS_DIRECTORY / owner_id


def _receipt_path(state_root: Path | str, owner_id: str, receipt_id: str) -> Path:
    if not isinstance(receipt_id, str) or not _RECEIPT_ID.fullmatch(receipt_id):
        raise ContextReceiptError("context receipt id is invalid")
    return _owner_receipt_dir(state_root, owner_id) / f"{receipt_id}.json"


def _atomic_private_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    os.chmod(path.parent, 0o700)
    payload = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", dir=path.parent, text=True
    )
    temporary = Path(temporary_name)
    try:
        os.chmod(temporary, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _read_snapshot(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ContextReceiptError("context receipt snapshot cannot be read") from exc
    if not isinstance(value, dict):
        raise ContextReceiptError("context receipt snapshot is not an object")
    if set(value) != _SNAPSHOT_FIELDS:
        raise ContextReceiptError("context receipt snapshot fields do not match its contract")
    if value.get("schema_version") != CONTEXT_RECEIPT_SCHEMA_VERSION:
        raise ContextReceiptError("context receipt snapshot version is not supported")
    if not isinstance(value.get("receipt_id"), str) or not _RECEIPT_ID.fullmatch(value["receipt_id"]):
        raise ContextReceiptError("context receipt snapshot id is invalid")
    if not isinstance(value.get("owner"), str) or not value["owner"]:
        raise ContextReceiptError("context receipt snapshot owner binding is invalid")
    if not isinstance(value.get("plan_id"), str) or not value["plan_id"]:
        raise ContextReceiptError("context receipt snapshot plan id is invalid")
    version = value.get("plan_version")
    if isinstance(version, bool) or not isinstance(version, int) or version < 1:
        raise ContextReceiptError("context receipt snapshot plan version is invalid")
    if value.get("context_id") is not None and not isinstance(value.get("context_id"), str):
        raise ContextReceiptError("context receipt snapshot context id is invalid")
    if not isinstance(value.get("context_hash"), str) or not value["context_hash"]:
        raise ContextReceiptError("context receipt snapshot context hash is invalid")
    if not isinstance(value.get("context"), dict):
        raise ContextReceiptError("context receipt snapshot context is not an object")
    _parse_instant(value.get("issued_at"))
    _parse_instant(value.get("expires_at"))
    if canonical_hash(value["context"]) != value["context_hash"]:
        raise ContextReceiptError("context receipt snapshot content was tampered with")
    return value


def prune_expired_context_receipts(
    state_root: Path | str, owner_id: str, *, now: dt.datetime
) -> int:
    """Remove only valid, expired snapshots for this owner.

    An unreadable file is retained so a later use fails closed instead of being silently
    reclassified as a missing receipt.  Cleanup is opportunistic; expiry remains enforced
    by ``open_context_receipt`` even if cleanup never runs.
    """
    directory = _owner_receipt_dir(state_root, owner_id)
    if not directory.is_dir():
        return 0
    current = now.astimezone(dt.timezone.utc)
    removed = 0
    for path in directory.glob("ctx-*.json"):
        try:
            snapshot = _read_snapshot(path)
        except ContextReceiptError:
            continue
        if current >= _parse_instant(snapshot["expires_at"]):
            path.unlink(missing_ok=True)
            removed += 1
    return removed


def issue_context_receipt(
    state_root: Path | str,
    *,
    owner_id: str,
    owner_binding: str,
    plan_id: str,
    plan_version: int,
    context: dict[str, Any],
    key: bytes,
    now: dt.datetime,
    ttl_seconds: int = CONTEXT_RECEIPT_TTL_SECONDS,
) -> dict[str, str]:
    """Persist one exact context snapshot and return its signed opaque receipt."""
    if not isinstance(context, dict):
        raise ContextReceiptError("context snapshot must be an object")
    if not isinstance(owner_binding, str) or not owner_binding:
        raise ContextReceiptError("context snapshot owner binding is invalid")
    if not isinstance(plan_id, str) or not plan_id:
        raise ContextReceiptError("context snapshot plan id is invalid")
    if isinstance(plan_version, bool) or not isinstance(plan_version, int) or plan_version < 1:
        raise ContextReceiptError("context snapshot plan version is invalid")

    prune_expired_context_receipts(state_root, owner_id, now=now)
    receipt_id = "ctx-" + secrets.token_urlsafe(18)
    context_hash = canonical_hash(context)
    issued = now.astimezone(dt.timezone.utc).replace(microsecond=0)
    issued_proposal = issue_proposal(
        {
            "kind": "context_receipt",
            "owner": owner_binding,
            "receipt_id": receipt_id,
            "plan_id": plan_id,
            "plan_version": plan_version,
            "context_id": context.get("context_id") if isinstance(context.get("context_id"), str) else None,
            "context_hash": context_hash,
        },
        key=key,
        now=issued,
        ttl_seconds=ttl_seconds,
    )
    snapshot = {
        "schema_version": CONTEXT_RECEIPT_SCHEMA_VERSION,
        "receipt_id": receipt_id,
        "owner": owner_binding,
        "plan_id": plan_id,
        "plan_version": plan_version,
        "context_id": context.get("context_id") if isinstance(context.get("context_id"), str) else None,
        "context_hash": context_hash,
        "issued_at": issued_proposal["issued_at"],
        "expires_at": issued_proposal["expires_at"],
        "context": copy.deepcopy(context),
    }
    _atomic_private_json(_receipt_path(state_root, owner_id, receipt_id), snapshot)
    return {
        "receipt": issued_proposal["proposal"],
        "expires_at": issued_proposal["expires_at"],
    }


def open_context_receipt(
    receipt: Any,
    state_root: Path | str,
    *,
    owner_id: str,
    owner_binding: str,
    key: bytes,
    now: dt.datetime,
) -> dict[str, Any]:
    """Verify the signed handle and exact private snapshot for one owner."""
    try:
        opened = open_proposal(receipt, key=key, now=now)
    except ProposalError as exc:
        raise ContextReceiptError("context receipt is not valid for this gateway") from exc
    claims = opened["claims"]
    if claims.get("kind") != "context_receipt" or claims.get("owner") != owner_binding:
        raise ContextReceiptError("context receipt owner or kind does not match")
    receipt_id = claims.get("receipt_id")
    path = _receipt_path(state_root, owner_id, receipt_id)
    snapshot = _read_snapshot(path)
    for field in (
        "receipt_id",
        "owner",
        "plan_id",
        "plan_version",
        "context_id",
        "context_hash",
    ):
        if claims.get(field) != snapshot.get(field):
            raise ContextReceiptError("context receipt binding does not match its snapshot")
    if claims.get("issued_at") != snapshot.get("issued_at") or claims.get("expires_at") != snapshot.get("expires_at"):
        raise ContextReceiptError("context receipt lifetime does not match its snapshot")
    expired = bool(opened["expired"] or now.astimezone(dt.timezone.utc) >= _parse_instant(snapshot["expires_at"]))
    return {
        "context": copy.deepcopy(snapshot["context"]),
        "context_hash": snapshot["context_hash"],
        "context_id": snapshot["context_id"],
        "plan_id": snapshot["plan_id"],
        "plan_version": snapshot["plan_version"],
        "receipt_id": snapshot["receipt_id"],
        "expires_at": snapshot["expires_at"],
        "expired": expired,
    }
