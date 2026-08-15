"""Private, append-only local state for continuous Coach Loop use.

The store keeps only PlanState versions, DecisionEvents, and integrity receipts. It
never persists CoachContext, provider credentials, raw payloads, GPS, FIT activities,
or connection state.
"""

from __future__ import annotations

import copy
import datetime as dt
import hashlib
import json
import os
import re
import shutil
import tempfile
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

# The athlete's timezone is defined once, where the context window already resolves it.
from .context_core import DEFAULT_TIMEZONE

from .delivery_content import delivery_session_content
from .validation import (
    ACTIONABLE_MATCH_STATUSES,
    validate_bundle,
    validate_decision_event,
    validate_plan_state,
)


STORE_SCHEMA_VERSION = "1.0"

# The forward-only guard version (issue #88): what this checkout writes into a store's
# PlanState, DecisionEvent, and manifest shape. Deliberately independent of
# STORE_SCHEMA_VERSION above and the *_SCHEMA_VERSION constants in validation.py -- those
# have stayed at "1.0" through several additive field changes, so they are not the number
# CLAUDE.md's "land the schema change on main" warning is actually about. This is: every
# write path compares it against the value recorded in the store before touching anything
# (see _inspect_store), and it is the one that must move in the same change that lands a
# PlanState/DecisionEvent shape change an older checkout could not safely open or extend.
# A purely additive, optional field does not need a bump.
#
# 2 (issue #93): `session.plan` became a required discriminated union and
# `session.prescription` a required rendering of it, while `structured_workout` and
# `strength_movements` stopped being accepted at all. Neither direction survives the
# change -- a store written under 1 has no `plan` for this code to read, and a store
# written under 2 carries a `plan` older code rejects as an unknown field -- so this is
# exactly the shape change the number exists to announce. Not a compatibility shim:
# stored plans under 1 still do not open, and the athlete regenerates once. What the bump
# buys is that the failure says so.
# 3 (issue #113): `session.execution` may carry `superseded_external_id`, the Intervals
# event a confirmed change left on the calendar. Optional and additive, which would not by
# itself need a bump -- except that `validate_plan_state` refuses an unexpected key inside
# `execution`, so a store where any session carries it does not open under a checkout that
# has not been taught the field. Not the new field failing: the whole store. That is the
# direction this number exists to announce.
WRITER_CONTRACT_VERSION = 3

# One delivery may be writing to Intervals at a time, and while it is, the plan it was
# bound to may not change underneath it (issue #110). The reservation lives in a file
# beside the manifest rather than in memory: the boundary that has to hold is the store,
# not the process, and a delivery interrupted mid-set has to leave what Intervals already
# accepted somewhere a later run can read.
#
# Schema 2.0 (issue #121) turns the reservation into a journal of one operation per
# session. Version 1.0 recorded only `verified` items, which is one state too late: the
# external effect happens at the provider write, not at the read-back that follows it, so
# a write that landed and then failed verification left nothing on disk at all. Forward
# only, deliberately: a 1.0 file is refused rather than migrated (see
# `_read_delivery_attempt`), because guessing which operations a 1.0 file had already
# started is exactly the guess this schema exists to stop.
DELIVERY_ATTEMPT_FILE = "delivery-attempt.json"

# What the athlete said that no device holds -- availability and reported lifts (issues
# #28, #47). Owned by ``athlete_evidence``; named here because an owner directory's own
# contents are this module's business: ``init_store`` has to know which of them may
# legitimately predate a plan, and nothing else in the directory may.
ATHLETE_EVIDENCE_FILE = "athlete-evidence.json"
DELIVERY_ATTEMPT_SCHEMA_VERSION = "2.0"

# The life of one provider operation inside an attempt, in order.
#
#   not_started         nothing has been said to Intervals for this session yet.
#   mutation_started    the mutating call is about to be made, or was made and never
#                       answered. Intervals may or may not hold the effect.
#   mutated_unverified  Intervals answered the mutation -- an event id, or a delete
#                       acknowledgement -- and the effect is not yet verified.
#   verified            the exact provider read-back matched. PlanState does not say so.
#   recorded            PlanState records it. Nothing is outstanding for this operation.
#
# Everything between `not_started` and `recorded` is an external effect this store has not
# reconciled, so it is what keeps the reservation open (`unresolved_delivery_operations`).
DELIVERY_OPERATION_STATES = (
    "not_started",
    "mutation_started",
    "mutated_unverified",
    "verified",
    "recorded",
)
_UNRESOLVED_OPERATION_STATES = frozenset(
    {"mutation_started", "mutated_unverified", "verified"}
)
DELIVERY_OPERATION_KINDS = ("upsert", "delete")
DELIVERY_ATTEMPT_KINDS = ("delivery", "withdrawal")

REPO_ROOT = Path(__file__).resolve().parents[1]
COMMIT_PATTERN = re.compile(r"^(\d{8})-(.+)$")


class StateStoreError(RuntimeError):
    """A deterministic state-store operation was blocked."""

    def __init__(self, message: str, *, details: Any | None = None):
        super().__init__(message)
        self.details = details


def default_state_dir() -> Path:
    configured = os.environ.get("GARMIN_COACH_LOOP_HOME")
    if configured:
        return Path(configured).expanduser()
    return Path.home() / ".local" / "share" / "garmin-coach-loop"


def resolve_state_root(state_root: Path | str) -> Path:
    """Resolve a root that holds one private state directory per owner.

    Same containment rule as any single-owner store -- state never lives inside the
    repository. Public so a multi-owner caller can fail at startup rather than on its
    first authenticated request.
    """
    return _state_root(state_root)


def resolve_state_dir(owner_id: str, *, state_root: Path | str) -> Path:
    """Map one owner id to the private state directory that owner alone may reach.

    ``owner_id`` must be the canonical string form of a UUID. Parsing it and then
    demanding ``str(parsed) == owner_id`` rejects path traversal, absolute paths, and
    every non-canonical spelling (braces, ``urn:uuid:``, upper case) in one check: the
    directory name can then only be one of the fixed-shape UUID strings, so no owner can
    ever be steered at another owner's state or outside the root.

    Deliberately unrelated to ``default_state_dir``. That one reads
    ``GARMIN_COACH_LOOP_HOME`` to answer "which store does this machine's CLI user own",
    which is not a question a multi-owner server is allowed to ask.
    """
    if not isinstance(owner_id, str) or not owner_id:
        raise StateStoreError("owner_id must be a non-empty string")
    try:
        parsed = uuid.UUID(owner_id)
    except ValueError as exc:
        raise StateStoreError("owner_id must be a canonical UUID string") from exc
    if str(parsed) != owner_id:
        raise StateStoreError("owner_id must be a canonical UUID string")
    return resolve_state_root(state_root) / "owners" / owner_id


def canonical_hash(value: Any) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def delivery_session_content_hash(session: dict[str, Any]) -> str:
    """Hash the single delivery-relevant session projection."""
    return canonical_hash(delivery_session_content(session))


def _state_root(path: Path | str) -> Path:
    root = Path(path).expanduser().resolve()
    try:
        root.relative_to(REPO_ROOT)
    except ValueError:
        return root
    raise StateStoreError("state directory must be outside the repository")


def _read_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise StateStoreError(f"cannot read {path.name}: {exc}") from exc
    if not isinstance(value, dict):
        raise StateStoreError(f"{path.name} must contain a JSON object")
    return value


def _write_new_json(path: Path, value: dict[str, Any]) -> None:
    payload = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError as exc:
        raise StateStoreError(f"refusing to overwrite append-only file {path.name}") from exc
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
    except Exception:
        path.unlink(missing_ok=True)
        raise


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    payload = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        dir=path.parent,
        text=True,
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


@contextmanager
def _exclusive_lock(root: Path) -> Iterator[None]:
    lock_path = root / ".lock"
    try:
        descriptor = os.open(lock_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError as exc:
        raise StateStoreError("state store is locked by another operation") from exc
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(f"pid={os.getpid()}\n")
            handle.flush()
            os.fsync(handle.fileno())
        yield
    finally:
        lock_path.unlink(missing_ok=True)


# What never travels with a copy of a store: the process lock, and the reservation for a
# delivery that is in flight *here*. Both describe this machine's current operation, not
# the athlete's history, and a snapshot or an adopted copy carrying one would fence writes
# on a store where nothing is actually being delivered.
#
# This exclusion is only honest because the operations that make copies now refuse to run
# at all while a reservation exists (issue #122): dropping a reservation from a copy taken
# *during* a provider write would advertise a recovery point that omits the one record of
# that write. See `_refuse_while_delivery_in_flight`.
_COPY_IGNORE = shutil.ignore_patterns(".lock", DELIVERY_ATTEMPT_FILE)


def _attempt_path(root: Path) -> Path:
    return root / DELIVERY_ATTEMPT_FILE


_ATTEMPT_FIELDS = {
    "schema_version",
    "attempt_id",
    "kind",
    "plan_id",
    "plan_version",
    "proposal_hash",
    "session_ids",
    "writer_contract_version",
    "opened_at",
    "recorded_plan_version",
    "operations",
}
_OPERATION_FIELDS = {
    "session_id",
    "operation",
    "state",
    "owned_external_id",
    "external_id",
    "scheduled_date",
    "detail",
    "result",
    "updated_at",
}


def _unreadable_attempt(reason: str) -> StateStoreError:
    """Fail closed on a reservation this code cannot read, and say how to clear it.

    Previously this file could disappear through a swallowed parse error, which is the
    worst possible reading of it: the one thing a reservation means is that Intervals may
    hold an effect this store has not recorded, and a file that cannot be parsed cannot
    rule that out. So it fences the store instead, and only a human who has read the
    calendar removes it (issue #122).
    """
    return StateStoreError(
        f"{DELIVERY_ATTEMPT_FILE} is not a delivery reservation this code can read: "
        f"{reason}. A reservation means Intervals may hold a workout this store never "
        "recorded, so the store stays fenced until it is gone: read the Intervals "
        "calendar for the current week, then run clear-delivery-attempt --confirm to "
        "remove it and re-run doctor-store.",
        details={"kind": "unreadable_delivery_attempt", "reason": reason},
    )


def _validated_attempt(value: dict[str, Any]) -> dict[str, Any]:
    version = value.get("schema_version")
    if version != DELIVERY_ATTEMPT_SCHEMA_VERSION:
        raise _unreadable_attempt(
            f"it declares schema_version {version!r}, and this code writes "
            f"{DELIVERY_ATTEMPT_SCHEMA_VERSION}"
        )
    if set(value) != _ATTEMPT_FIELDS:
        raise _unreadable_attempt(
            f"its fields do not match the contract; missing="
            f"{sorted(_ATTEMPT_FIELDS - set(value))}; extra={sorted(set(value) - _ATTEMPT_FIELDS)}"
        )
    if value.get("kind") not in DELIVERY_ATTEMPT_KINDS:
        raise _unreadable_attempt(f"kind {value.get('kind')!r} is not a delivery kind")
    for field in ("attempt_id", "plan_id", "proposal_hash", "opened_at"):
        if not isinstance(value.get(field), str) or not value[field].strip():
            raise _unreadable_attempt(f"{field} must be a non-empty string")
    for field in ("plan_version", "writer_contract_version"):
        number = value.get(field)
        if isinstance(number, bool) or not isinstance(number, int) or number < 1:
            raise _unreadable_attempt(f"{field} must be a positive integer")
    if value["writer_contract_version"] > WRITER_CONTRACT_VERSION:
        raise _unreadable_attempt(
            f"it was opened by writer-contract version {value['writer_contract_version']}, "
            f"newer than this code's {WRITER_CONTRACT_VERSION}"
        )
    recorded_version = value.get("recorded_plan_version")
    if recorded_version is not None and (
        isinstance(recorded_version, bool)
        or not isinstance(recorded_version, int)
        or recorded_version < 1
    ):
        raise _unreadable_attempt("recorded_plan_version must be null or a positive integer")
    operations = value.get("operations")
    if not isinstance(operations, list) or not operations:
        raise _unreadable_attempt("operations must be a non-empty array")
    for index, operation in enumerate(operations):
        if not isinstance(operation, dict) or set(operation) != _OPERATION_FIELDS:
            observed = set(operation) if isinstance(operation, dict) else set()
            raise _unreadable_attempt(
                f"operations[{index}] fields do not match the contract; missing="
                f"{sorted(_OPERATION_FIELDS - observed)}; extra={sorted(observed - _OPERATION_FIELDS)}"
            )
        if operation["state"] not in DELIVERY_OPERATION_STATES:
            raise _unreadable_attempt(
                f"operations[{index}] state {operation['state']!r} is not a known state"
            )
        if operation["operation"] not in DELIVERY_OPERATION_KINDS:
            raise _unreadable_attempt(
                f"operations[{index}] operation {operation['operation']!r} is not a known operation"
            )
        for field in ("session_id", "owned_external_id", "scheduled_date"):
            if not isinstance(operation.get(field), str) or not operation[field].strip():
                raise _unreadable_attempt(f"operations[{index}] {field} must be a non-empty string")
    session_ids = value.get("session_ids")
    if session_ids != sorted({operation["session_id"] for operation in operations}):
        raise _unreadable_attempt("session_ids does not match the journalled operations")
    return value


def _read_delivery_attempt(root: Path) -> dict[str, Any] | None:
    path = _attempt_path(root)
    if not path.exists():
        return None
    try:
        value = _read_object(path)
    except StateStoreError as exc:
        raise _unreadable_attempt(str(exc)) from exc
    return _validated_attempt(value)


def unresolved_delivery_operations(attempt: dict[str, Any]) -> list[dict[str, Any]]:
    """The operations of this attempt that Intervals may hold and this store has not recorded.

    `verified` counts: an exact read-back that no PlanState commit has recorded is still a
    calendar the plan does not describe. Only `not_started` (nothing was said to the
    provider) and `recorded` (the plan says it too) are settled.
    """
    return [
        operation
        for operation in attempt.get("operations") or []
        if operation.get("state") in _UNRESOLVED_OPERATION_STATES
    ]


def _operation_summary(operations: list[dict[str, Any]]) -> str:
    return ", ".join(
        f"{operation['session_id']} ({operation['operation']} {operation['state']}"
        + (f", Intervals event {operation['external_id']}" if operation.get("external_id") else "")
        + ")"
        for operation in operations
    )


def _delivery_attempt_conflict_message(attempt: dict[str, Any]) -> str:
    """The one wording for 'a provider write is in flight', used everywhere it can surface.

    A delivery holds this reservation from before its first Intervals write until the
    verified result is recorded. While it is open the plan may be read but not changed:
    a revision committed mid-flight would put content on the athlete's calendar that no
    version of PlanState ever described (issue #110).
    """
    sessions = ", ".join(attempt.get("session_ids") or []) or "unknown sessions"
    unresolved = unresolved_delivery_operations(attempt)
    outstanding = (
        f" Intervals may already hold {_operation_summary(unresolved)}."
        if unresolved
        else ""
    )
    return (
        f"a delivery to Intervals is in flight ({attempt.get('attempt_id')}, opened "
        f"{attempt.get('opened_at')}, sessions {sessions}); finish that delivery, or read "
        "the Intervals calendar and run clear-delivery-attempt, before changing state."
        + outstanding
    )


def _refuse_while_delivery_in_flight(root: Path, operation: str) -> None:
    """Refuse a maintenance operation that would fork, replace or advertise this store.

    Snapshot, restore and copy-adoption each produce a store that deliberately carries no
    reservation (`_COPY_IGNORE`). That is right for a store where nothing is in flight and
    wrong for one where something is: the copy would present itself as a recovery point
    while omitting the only record of a provider write still in the air, and the restore
    would install an unfenced store on top of a delivery whose network calls are still
    running (issue #122).

    Link adoption is deliberately not on this list: it does not copy anything, so the
    adopted path keeps referring to the same reservation file (see `adopt_store`).
    """
    attempt = _read_delivery_attempt(root)
    if attempt is None:
        return
    sessions = ", ".join(attempt.get("session_ids") or []) or "unknown sessions"
    unresolved = unresolved_delivery_operations(attempt)
    outstanding = (
        f" Intervals may already hold {_operation_summary(unresolved)}."
        if unresolved
        else " Nothing has reached Intervals yet."
    )
    raise StateStoreError(
        f"{operation} is refused while a delivery to Intervals is in flight "
        f"({attempt['attempt_id']}, opened {attempt['opened_at']}, sessions {sessions})."
        + outstanding
        + " Finish or retry that delivery, or read the Intervals calendar and run "
        "clear-delivery-attempt, first.",
        details=attempt,
    )


def _utc_stamp() -> str:
    return (
        dt.datetime.now(dt.timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def open_delivery_attempt(
    state_dir: Path | str,
    *,
    kind: str,
    plan_id: str,
    plan_version: int,
    proposal_hash: str,
    operations: list[dict[str, Any]],
) -> dict[str, Any]:
    """Reserve this store for one provider-writing delivery, before its first write.

    This is the only point where the full store check runs *ahead* of an Intervals
    mutation: a torn store, or one written under a newer writer contract, refuses here
    and no provider write happens at all. It deliberately does not hold the store lock
    across the network calls that follow -- unrelated reads keep working; only state
    changes are fenced, by the reservation file this leaves behind.

    Every operation the set may perform is journalled up front as `not_started`, with the
    product-owned marker it will act under. The alternative -- appending a row when a
    write begins -- cannot survive the failure it exists for: a process that dies inside
    the mutating call never appends anything.

    Re-opening the same proposal against the same current plan resumes the existing
    attempt rather than refusing it, so a retry after a crash is the same operation.
    """
    if kind not in DELIVERY_ATTEMPT_KINDS:
        raise StateStoreError(f"delivery attempt kind must be one of {list(DELIVERY_ATTEMPT_KINDS)}")
    root = _state_root(state_dir)
    if not root.is_dir():
        raise StateStoreError("state directory does not exist; run init-store first")
    journalled = _new_operations(operations)
    ordered = sorted({operation["session_id"] for operation in journalled})
    with _exclusive_lock(root):
        existing = _read_delivery_attempt(root)
        if existing is not None:
            if (
                existing["proposal_hash"] == proposal_hash
                and existing["plan_id"] == plan_id
                and existing["plan_version"] == plan_version
                and existing["kind"] == kind
                and existing["session_ids"] == ordered
            ):
                return existing
            raise StateStoreError(
                _delivery_attempt_conflict_message(existing), details=existing
            )
        doctor, before, _ = _inspect_store(root, ignore_lock=True)
        if doctor["status"] != "passed" or before is None:
            raise StateStoreError(_doctor_failure_message(doctor), details=doctor)
        if before.get("plan_id") != plan_id or before.get("version") != plan_version:
            # Marked, because this is the delivery boundary refusing a stale binding --
            # not the store failing. The caller re-raises it as a delivery block so the
            # athlete gets "re-preview this" rather than "your state is in conflict".
            detail = (
                "current plan_id changed after publish-set preview"
                if before.get("plan_id") != plan_id
                else "current PlanState version changed after publish-set preview"
            )
            raise StateStoreError(
                f"{detail}; the current plan is {before.get('plan_id')} version "
                f"{before.get('version')}",
                details={
                    "kind": "stale_plan_binding",
                    "plan_id": before.get("plan_id"),
                    "current_version": before.get("version"),
                },
            )
        attempt = {
            "schema_version": DELIVERY_ATTEMPT_SCHEMA_VERSION,
            "attempt_id": "delivery-attempt-"
            + canonical_hash(
                {
                    "plan_id": plan_id,
                    "plan_version": plan_version,
                    "proposal_hash": proposal_hash,
                    "session_ids": ordered,
                }
            )[:24],
            "kind": kind,
            "plan_id": plan_id,
            "plan_version": plan_version,
            "proposal_hash": proposal_hash,
            "session_ids": ordered,
            "writer_contract_version": WRITER_CONTRACT_VERSION,
            "opened_at": _utc_stamp(),
            "recorded_plan_version": None,
            "operations": journalled,
        }
        _atomic_json(_attempt_path(root), _validated_attempt(attempt))
    return attempt


def _new_operations(operations: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not isinstance(operations, list) or not operations:
        raise StateStoreError("delivery attempt must name at least one operation")
    journalled: list[dict[str, Any]] = []
    stamp = _utc_stamp()
    for operation in operations:
        if not isinstance(operation, dict):
            raise StateStoreError("delivery attempt operation must be an object")
        journalled.append(
            {
                "session_id": str(operation.get("session_id")),
                "operation": operation.get("operation"),
                "state": "not_started",
                "owned_external_id": str(operation.get("owned_external_id")),
                "external_id": (
                    str(operation["external_id"])
                    if operation.get("external_id") is not None
                    else None
                ),
                "scheduled_date": str(operation.get("scheduled_date")),
                "detail": None,
                "result": None,
                "updated_at": stamp,
            }
        )
    if len({operation["session_id"] for operation in journalled}) != len(journalled):
        raise StateStoreError("delivery attempt names the same session more than once")
    return sorted(journalled, key=lambda operation: operation["session_id"])


def record_delivery_attempt_operation(
    state_dir: Path | str,
    *,
    attempt_id: str,
    session_id: str,
    state: str,
    external_id: str | None = None,
    detail: str | None = None,
    result: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Move one operation of an open attempt to its next state, durably.

    Called on both sides of every mutating provider call, so an interruption anywhere --
    inside the request, between the response and the read-back, between the read-back and
    the PlanState commit -- leaves the store saying which session, which operation, which
    product-owned marker, and which provider event id are outstanding.

    ``external_id`` is never unlearned: a state that arrives without one keeps whatever the
    journal already knows, because the id of an event that may exist is the single most
    expensive thing to lose. The one exception is a return to ``not_started``, which is the
    provider itself saying the effect is not there.
    """
    if state not in DELIVERY_OPERATION_STATES:
        raise StateStoreError(f"delivery operation state must be one of {list(DELIVERY_OPERATION_STATES)}")
    root = _state_root(state_dir)
    with _exclusive_lock(root):
        attempt = _read_delivery_attempt(root)
        if attempt is None or attempt["attempt_id"] != attempt_id:
            raise StateStoreError("no such delivery attempt is open")
        operation = next(
            (item for item in attempt["operations"] if item["session_id"] == session_id),
            None,
        )
        if operation is None:
            raise StateStoreError(f"delivery attempt does not cover session {session_id}")
        operation["state"] = state
        if state == "not_started":
            operation["external_id"] = None
            operation["result"] = None
        elif external_id is not None:
            operation["external_id"] = str(external_id)
        operation["detail"] = detail
        if result is not None:
            operation["result"] = copy.deepcopy(result)
        operation["updated_at"] = _utc_stamp()
        _atomic_json(_attempt_path(root), _validated_attempt(attempt))
    return attempt


def mark_delivery_attempt_recorded(
    state_dir: Path | str,
    *,
    attempt_id: str,
    session_ids: list[str],
    plan_version: int,
) -> dict[str, Any]:
    """Mark the operations a PlanState commit has just recorded, and the version it wrote.

    The commit and this mark are two writes, so a process can die between them. That is
    survivable and does not need a transaction: the retry re-reads the plan and promotes
    any operation the plan already records (see ``delivery``'s reconciliation). What the
    mark buys is that the common path does not have to.
    """
    root = _state_root(state_dir)
    with _exclusive_lock(root):
        attempt = _read_delivery_attempt(root)
        if attempt is None or attempt["attempt_id"] != attempt_id:
            raise StateStoreError("no such delivery attempt is open")
        wanted = set(session_ids)
        stamp = _utc_stamp()
        for operation in attempt["operations"]:
            if operation["session_id"] in wanted:
                operation["state"] = "recorded"
                operation["updated_at"] = stamp
        attempt["recorded_plan_version"] = plan_version
        _atomic_json(_attempt_path(root), _validated_attempt(attempt))
    return attempt


def close_delivery_attempt(
    state_dir: Path | str,
    *,
    attempt_id: str | None = None,
    abandon_unresolved: bool = False,
) -> dict[str, Any]:
    """Release the reservation. Without an attempt_id this is the operator's recovery path.

    With an ``attempt_id`` this is a delivery closing its own reservation, and it refuses
    while any operation is unresolved: closing there is exactly the loss issue #121 is
    about. Without one it is the human saying they have read the Intervals calendar and
    taken responsibility for whatever the journal still names, so it clears anything --
    including a file this code cannot parse -- and reports what was abandoned.

    ``abandon_unresolved`` is the third caller: a person who is taking that same
    responsibility from somewhere that has no filesystem, and who therefore has to prove
    which reservation they were looking at. It requires an ``attempt_id``, and checks it
    inside the same lock that does the removal, so a reservation opened between reading
    the id and clearing it is never the one that gets cleared.
    """
    if abandon_unresolved and attempt_id is None:
        raise StateStoreError(
            "abandoning a delivery reservation's unresolved operations must name the "
            "attempt being released"
        )
    root = _state_root(state_dir)
    with _exclusive_lock(root):
        unreadable: str | None = None
        try:
            attempt = _read_delivery_attempt(root)
        except StateStoreError as exc:
            if attempt_id is not None:
                # A delivery may only close the reservation it can prove is its own, and
                # a reservation this code cannot parse proves nothing about which one it
                # is. Clearing that one stays the local operator's call.
                raise
            attempt, unreadable = None, str(exc)
        if attempt is None and unreadable is None:
            return {"status": "passed", "cleared": False, "attempt": None, "abandoned": []}
        abandoned: list[dict[str, Any]] = []
        if attempt is not None:
            if attempt_id is not None and attempt["attempt_id"] != attempt_id:
                raise StateStoreError(
                    _delivery_attempt_conflict_message(attempt), details=attempt
                )
            outstanding = unresolved_delivery_operations(attempt)
            if attempt_id is not None and outstanding and not abandon_unresolved:
                raise StateStoreError(
                    "refusing to release a delivery reservation that still holds "
                    f"unreconciled Intervals effects: {_operation_summary(outstanding)}",
                    details=attempt,
                )
            abandoned = [
                {
                    "session_id": operation["session_id"],
                    "operation": operation["operation"],
                    "state": operation["state"],
                    "external_id": operation["external_id"],
                    "owned_external_id": operation["owned_external_id"],
                }
                for operation in outstanding
            ]
        _attempt_path(root).unlink(missing_ok=True)
    result = {"status": "passed", "cleared": True, "attempt": attempt, "abandoned": abandoned}
    if unreadable is not None:
        result["unreadable"] = unreadable
    return result


def pending_delivery_attempt(state_dir: Path | str) -> dict[str, Any] | None:
    """The delivery reservation this store currently holds, if any.

    Raises rather than answering ``None`` when the file exists and cannot be read: absent
    and unreadable are different facts, and only one of them means no provider write is
    outstanding (AGENTS.md 3).
    """
    root = _state_root(state_dir)
    if not root.is_dir():
        return None
    return _read_delivery_attempt(root)


def _writer_contract_conflict_message(store_version: int) -> str:
    """The one wording for 'this store outran this code', used everywhere it can surface.

    ``_inspect_store`` puts it in a doctor report's ``errors``; the write paths put it as
    the raised exception's own top-level message, not just in ``details`` -- the gateway's
    generic ``StateStoreError`` handler forwards only ``str(exc)`` to the athlete side, so
    the actionable text has to live there to ever reach a Custom GPT response.
    """
    return (
        f"store writer-contract version {store_version} is newer than this code's "
        f"writer-contract version {WRITER_CONTRACT_VERSION}; pull a checkout that supports "
        f"writer-contract version {store_version} or later before reading or writing this "
        "store."
    )


def _writer_contract_upgrade_message(store_version: int) -> str:
    """The one wording for 'this code outran the store' -- the far more common direction.

    Mirrors ``_writer_contract_conflict_message`` for the opposite side, with one
    difference in how it is used: an older store is not refused on this fact alone (see
    ``_inspect_store``), because this checkout was taught its shape and a store whose
    data still fits the current shape opens and writes normally, snapshot included. This
    wording is only ever surfaced once doctor's full pass has *also* found something
    wrong, as the leading line of that report rather than the only one -- a version
    mismatch explains itself; a page of schema errors that all trace back to it does not.
    """
    return (
        f"store writer-contract version {store_version} is older than this code's "
        f"writer-contract version {WRITER_CONTRACT_VERSION}; this code cannot open its "
        "stored history as-is, so regenerate the store under this checkout, or use a "
        f"checkout that still supports writer-contract version {store_version}, before "
        "reading or writing it further."
    )


def _doctor_failure_message(doctor: dict[str, Any]) -> str:
    """The top-level message a write path should raise for a blocked doctor report.

    Both directions of a writer-contract mismatch are common enough, and fixable enough
    by an operator reading only the exception message, that each gets its own wording;
    every other doctor failure (tampering, an incomplete pending commit, and so on) keeps
    the existing generic message, with the specifics carried in ``details`` as before. The
    older-than-code branch is only ever reached from an already-blocked report (see
    ``_inspect_store``), so the mismatch is known to actually be part of why.
    """
    version = doctor.get("writer_contract_version")
    if isinstance(version, int) and not isinstance(version, bool):
        if version > WRITER_CONTRACT_VERSION:
            return _writer_contract_conflict_message(version)
        if version < WRITER_CONTRACT_VERSION:
            return _writer_contract_upgrade_message(version)
    return "state store failed doctor"


def _commit_slug(event_id: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9._-]+", "-", event_id).strip("-._")
    return (slug or "decision")[:64]


def _commit_name(sequence: int, suffix: str) -> str:
    return f"{sequence:08d}-{suffix}"


def _write_commit(
    root: Path,
    *,
    sequence: int,
    plan: dict[str, Any],
    event: dict[str, Any] | None,
    context_hash: str | None,
) -> tuple[str, dict[str, Any]]:
    suffix = "initial" if event is None else _commit_slug(str(event["event_id"]))
    name = _commit_name(sequence, suffix)
    commits = root / "commits"
    final = commits / name
    if final.exists():
        raise StateStoreError(f"append-only commit already exists: {name}")
    pending = commits / f".pending-{uuid.uuid4().hex}"
    pending.mkdir(mode=0o700)
    created_at = (
        dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")
        if event is None
        else str(event["created_at"])
    )
    receipt = {
        "schema_version": STORE_SCHEMA_VERSION,
        "sequence": sequence,
        "kind": "initial" if event is None else "decision",
        "plan_id": plan["plan_id"],
        "plan_version": plan["version"],
        "event_id": None if event is None else event["event_id"],
        "context_hash": context_hash,
        "plan_hash": canonical_hash(plan),
        "event_hash": None if event is None else canonical_hash(event),
        "created_at": created_at,
    }
    receipt["receipt_hash"] = canonical_hash(receipt)
    try:
        _write_new_json(pending / "plan.json", plan)
        if event is not None:
            _write_new_json(pending / "event.json", event)
        _write_new_json(pending / "receipt.json", receipt)
        os.replace(pending, final)
    except Exception:
        for child in pending.iterdir() if pending.exists() else ():
            child.unlink(missing_ok=True)
        if pending.exists():
            pending.rmdir()
        raise
    return name, receipt


def _manifest(
    *,
    plan_id: str,
    sequence: int,
    version: int,
    commit_name: str,
    created_at: str,
    updated_at: str,
) -> dict[str, Any]:
    return {
        "schema_version": STORE_SCHEMA_VERSION,
        "plan_id": plan_id,
        "current_sequence": sequence,
        "current_version": version,
        "current_commit": commit_name,
        "created_at": created_at,
        "updated_at": updated_at,
        "stores_coach_context": False,
        "stores_provider_state": False,
        "writer_contract_version": WRITER_CONTRACT_VERSION,
    }


def _delivery_transition_errors(
    before: dict[str, Any],
    after: dict[str, Any],
    event: dict[str, Any],
) -> list[str]:
    """Fence the deterministic delivery observation stored in PlanState.

    Delivery is not a coaching decision.  The only allowed mutation is recording the
    Intervals event that was written and then read back successfully.  Keeping this
    fence beside the append-only writer makes both new writes and historical replay
    enforce the same narrow transition.
    """

    errors: list[str] = []
    if event.get("mode") != "record_delivery" or event.get("action") != "record":
        errors.append("delivery event requires mode record_delivery and action record")
    if event.get("reason_codes") != ["delivery_verified"]:
        errors.append("delivery event requires only reason code delivery_verified")
    if before.get("plan_id") != after.get("plan_id") or event.get("plan_id") != before.get("plan_id"):
        errors.append("delivery event must preserve and bind plan_id")
    if event.get("plan_version_before") != before.get("version"):
        errors.append("delivery event before version mismatch")
    if event.get("plan_version_after") != after.get("version"):
        errors.append("delivery event after version mismatch")
    if after.get("version") != before.get("version", 0) + 1:
        errors.append("delivery event must increment PlanState version exactly once")

    for field in ("schema_version", "status", "goal", "cycle", "athlete_baseline"):
        if before.get(field) != after.get(field):
            errors.append(f"delivery event must not change {field}")
    before_week = before.get("week") if isinstance(before.get("week"), dict) else {}
    after_week = after.get("week") if isinstance(after.get("week"), dict) else {}
    for field in set(before_week) | set(after_week):
        if field != "sessions" and before_week.get(field) != after_week.get(field):
            errors.append(f"delivery event must not change week.{field}")

    before_sessions = _sessions_by_id(before)
    after_sessions = _sessions_by_id(after)
    if set(before_sessions) != set(after_sessions):
        errors.append("delivery event must preserve the weekly session set")
        return errors
    changed = [
        candidate
        for candidate in before_sessions
        if before_sessions[candidate] != after_sessions[candidate]
    ]
    if not changed:
        errors.append("delivery event must change at least one session")
        return errors
    session_id = event.get("session_id")
    if len(changed) == 1 and session_id != changed[0]:
        errors.append("single-session delivery event must bind its changed session_id")
    if len(changed) > 1 and session_id is not None:
        errors.append("multi-session delivery event must use a null session_id")

    for changed_id in changed:
        before_session = before_sessions[changed_id]
        after_session = after_sessions[changed_id]
        for field in set(before_session) | set(after_session):
            if field != "execution" and before_session.get(field) != after_session.get(field):
                errors.append(f"delivery event must not change session field {field}")
        before_execution = before_session.get("execution") or {}
        after_execution = after_session.get("execution") or {}
        if before_execution.get("publish_supported") is not True:
            errors.append(f"delivery session {changed_id} must declare publish_supported=true")
        if (
            before_execution.get("delivery_state") != "not_published"
            or before_execution.get("external_id") is not None
        ):
            errors.append(
                f"delivery transition for {changed_id} must start at not_published with no external_id"
            )
        external_id = after_execution.get("external_id")
        if (
            after_execution.get("delivery_state") != "intervals_accepted"
            or not isinstance(external_id, str)
            or not external_id
        ):
            errors.append(
                f"delivery transition for {changed_id} must end at intervals_accepted with an external_id"
            )
        expected_execution = {
            **before_execution,
            "delivery_state": "intervals_accepted",
            "external_id": external_id,
        }
        if before_execution.get("superseded_external_id") == external_id:
            # The replacement was written to the very event that had been superseded, so
            # the calendar no longer holds an instruction the plan disagrees with. A
            # superseded id that is *not* this event survives: that orphan is still there.
            expected_execution.pop("superseded_external_id", None)
        if after_execution != expected_execution:
            errors.append(
                f"delivery event may only set delivery_state and external_id for {changed_id}"
            )
    return errors


def init_store(state_dir: Path | str, plan: dict[str, Any]) -> dict[str, Any]:
    root = _state_root(state_dir)
    validation = validate_plan_state(plan)
    if validation["status"] != "passed":
        raise StateStoreError("initial PlanState is invalid", details=validation)
    if plan.get("version") != 1:
        raise StateStoreError("initial PlanState version must be 1")
    # An athlete may state which days they train before deciding what to train on them
    # (issue #28), so the one file that can legitimately predate a plan does not count as
    # "already in use". Everything else still refuses: a commit chain, a manifest, or any
    # stray file means initializing here would discard or silently adopt state this code
    # did not write.
    if root.exists() and (
        not root.is_dir()
        or any(path.name != ATHLETE_EVIDENCE_FILE for path in root.iterdir())
    ):
        raise StateStoreError("state directory already exists and is not empty")
    root.mkdir(parents=True, mode=0o700, exist_ok=True)
    os.chmod(root, 0o700)
    (root / "commits").mkdir(mode=0o700)
    with _exclusive_lock(root):
        commit_name, receipt = _write_commit(
            root,
            sequence=1,
            plan=plan,
            event=None,
            context_hash=None,
        )
        manifest = _manifest(
            plan_id=plan["plan_id"],
            sequence=1,
            version=1,
            commit_name=commit_name,
            created_at=receipt["created_at"],
            updated_at=receipt["created_at"],
        )
        _atomic_json(root / "store.json", manifest)
    return {
        "status": "initialized",
        "policy": "private_repo_external_current_state",
        "plan_id": plan["plan_id"],
        "current_version": 1,
        "event_count": 0,
        "warnings": validation["warnings"],
    }


ADOPTION_MODES = ("link", "copy")


def adopt_store(
    source: Path | str,
    destination: Path | str,
    *,
    mode: str = "link",
    confirm: bool = False,
) -> dict[str, Any]:
    """Give an existing valid store a second home, without rewriting either one.

    ``init_store`` answers "there is no plan yet, here is the first one". This answers a
    different question: a store already exists somewhere, and some other path -- a
    gateway owner directory -- must now open *that* store. Re-initialising from its
    current plan would silently drop every earlier version, so nothing here writes a
    commit at all: ``link`` points the destination at the source, ``copy`` duplicates the
    whole append-only chain and then reopens it to prove the duplicate is sound.

    The difference matters and is the caller's to make: a link keeps one plan that both
    paths read and write, a copy creates a second plan that starts identical and diverges
    from the next decision onward.

    ``confirm=False`` runs every check and writes nothing, so the same code path decides
    what a preview promises and what the write does. A destination that already holds
    anything is refused in both, always: adopting is not merging.
    """
    if mode not in ADOPTION_MODES:
        raise StateStoreError(f"adoption mode must be one of {list(ADOPTION_MODES)}")

    source_root = _state_root(source)
    # The final path component is kept verbatim: resolving it would follow an existing
    # symlink and hide the very collision this refuses to write over.
    destination_path = Path(destination).expanduser()
    if destination_path.parent == destination_path:
        raise StateStoreError("destination must name a directory inside a state root")
    destination_root = _state_root(destination_path.parent) / destination_path.name
    if source_root == destination_root:
        raise StateStoreError("source and destination are the same store")

    report = doctor_store(source_root)
    if report["status"] != "passed":
        raise StateStoreError("source is not a valid PlanState store", details=report)
    if destination_root.exists() or destination_root.is_symlink():
        raise StateStoreError("destination already exists; refusing to overwrite or merge it")
    if mode == "copy":
        # A copy forks the plan while the source may still be putting an event on the one
        # shared Intervals calendar. The two stores would disagree about that calendar
        # from their first moment (issue #122). A link cannot: it is the same store.
        _refuse_while_delivery_in_flight(source_root, "adopt-owner-store --mode copy")
    source_attempt = _read_delivery_attempt(source_root)

    result = {
        "status": "preview" if not confirm else "adopted",
        "mode": mode,
        "source": str(source_root),
        "destination": str(destination_root),
        "plan_id": report["plan_id"],
        "current_version": report["current_version"],
        "event_count": report["event_count"],
        "policy": "private_repo_external_current_state",
    }
    if not confirm:
        return result

    destination_root.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    if mode == "link":
        destination_root.symlink_to(source_root, target_is_directory=True)
    else:
        # A lock is one operation's transient claim on one store, never part of its
        # content; copying one forward would leave the duplicate permanently locked.
        shutil.copytree(source_root, destination_root, ignore=_COPY_IGNORE)
        os.chmod(destination_root, 0o700)
    adopted = doctor_store(destination_root)
    failure: dict[str, Any] | None = (
        adopted if adopted["status"] != "passed" else None
    )
    if failure is None and mode == "link":
        # A link is only allowed to keep an open reservation because it demonstrably keeps
        # *the same* one. Proved rather than assumed: the adopted path must resolve to the
        # same reservation file and report the same attempt id, or the link is undone.
        adopted_attempt = _read_delivery_attempt(destination_root)
        same_file = _attempt_path(destination_root).resolve() == _attempt_path(source_root).resolve()
        if not same_file or (source_attempt or {}).get("attempt_id") != (
            adopted_attempt or {}
        ).get("attempt_id"):
            failure = {
                "status": "blocked",
                "errors": ["the adopted path does not reference the source's delivery reservation"],
            }
        elif adopted_attempt is not None:
            result["pending_delivery_attempt"] = adopted_attempt["attempt_id"]
    if failure is not None:
        if mode == "link":
            destination_root.unlink()
        else:
            shutil.rmtree(destination_root, ignore_errors=True)
        raise StateStoreError("adopted store does not open; nothing was kept", details=failure)
    return result


def _inspect_store(
    state_dir: Path | str,
    *,
    ignore_lock: bool = False,
) -> tuple[dict[str, Any], dict[str, Any] | None, dict[str, dict[str, Any]]]:
    root = _state_root(state_dir)
    errors: list[str] = []
    warnings: list[str] = []
    if not root.is_dir():
        return (
            {"status": "blocked", "errors": ["state directory does not exist"], "warnings": []},
            None,
            {},
        )
    if (root / ".lock").exists() and not ignore_lock:
        errors.append("state store is locked by another operation")
    try:
        manifest = _read_object(root / "store.json")
    except StateStoreError as exc:
        return {"status": "blocked", "errors": [str(exc)], "warnings": warnings}, None, {}

    # writer_contract_version arrived with issue #88; a manifest written before that has
    # no such field, and never having recorded one *is* contract version 1 -- the only
    # contract that has ever existed until some store carries a higher number.
    raw_contract_version = manifest.get("writer_contract_version", 1)
    if (
        isinstance(raw_contract_version, bool)
        or not isinstance(raw_contract_version, int)
        or raw_contract_version < 1
    ):
        observed_contract_version = None
    else:
        observed_contract_version = raw_contract_version

    if observed_contract_version is not None and observed_contract_version > WRITER_CONTRACT_VERSION:
        # A store written under a newer contract may hold fields or transitions this
        # checkout was never taught. Walking its history with today's validators would
        # not produce a trustworthy answer, only a confusing one -- so this refuses
        # before spending that effort, and before any write path gets near a commit.
        return (
            {
                "status": "blocked",
                "errors": [_writer_contract_conflict_message(observed_contract_version)],
                "warnings": [],
                "plan_id": manifest.get("plan_id"),
                "current_version": manifest.get("current_version"),
                "event_count": None,
                "policy": "private_repo_external_current_state",
                "writer_contract_version": observed_contract_version,
            },
            None,
            {},
        )

    # The opposite direction (issue #101): a store written under an older contract,
    # opened by this code -- the athlete's own upgrade, and the common one. Unlike a
    # newer store, this checkout was taught this shape (it is a subset of what it writes
    # today), so there is no reason to refuse on the version field alone: doctor's full
    # pass below still runs, a torn store still fails it whatever version it claims, and
    # a store whose data already fits the current shape still opens cleanly and writes
    # normally, snapshot included (see ``apply_decision``). All this narrow pre-check
    # does -- from the manifest field alone, before a single commit is opened -- is
    # precompute the sentence that pass should lead with *if* it finds something wrong,
    # so the actionable finding does not end up buried after a page of schema errors
    # that all trace back to the same cause. It is applied below, only once ``errors``
    # is known not to be empty.
    leading_error = (
        _writer_contract_upgrade_message(observed_contract_version)
        if observed_contract_version is not None and observed_contract_version < WRITER_CONTRACT_VERSION
        else None
    )

    required_manifest = {
        "schema_version",
        "plan_id",
        "current_sequence",
        "current_version",
        "current_commit",
        "created_at",
        "updated_at",
        "stores_coach_context",
        "stores_provider_state",
    }
    # writer_contract_version is the one manifest field allowed to be absent; see above.
    optional_manifest = {"writer_contract_version"}
    manifest_fields = set(manifest)
    if required_manifest - manifest_fields or manifest_fields - required_manifest - optional_manifest:
        errors.append("store.json fields do not match the V1 manifest")
    if observed_contract_version is None:
        errors.append("store.json writer_contract_version must be a positive integer")
    if manifest.get("schema_version") != STORE_SCHEMA_VERSION:
        errors.append(f"store schema_version must be {STORE_SCHEMA_VERSION}")
    if manifest.get("stores_coach_context") is not False:
        errors.append("store must not retain CoachContext")
    if manifest.get("stores_provider_state") is not False:
        errors.append("store must not retain provider state")

    commits_dir = root / "commits"
    pending_paths = sorted(commits_dir.glob(".pending-*")) if commits_dir.is_dir() else []
    if pending_paths:
        errors.append("store contains an incomplete pending commit")
    commit_paths = sorted(
        path for path in commits_dir.iterdir()
        if path.is_dir() and COMMIT_PATTERN.match(path.name)
    ) if commits_dir.is_dir() else []
    if not commit_paths:
        errors.append("store has no immutable commits")
    expected_sequences = list(range(1, len(commit_paths) + 1))
    observed_sequences: list[int] = []
    previous_plan: dict[str, Any] | None = None
    current_plan: dict[str, Any] | None = None
    event_index: dict[str, dict[str, Any]] = {}

    for path in commit_paths:
        match = COMMIT_PATTERN.match(path.name)
        assert match is not None
        sequence = int(match.group(1))
        observed_sequences.append(sequence)
        try:
            plan = _read_object(path / "plan.json")
            receipt = _read_object(path / "receipt.json")
        except StateStoreError as exc:
            errors.append(f"{path.name}: {exc}")
            continue
        plan_report = validate_plan_state(plan)
        errors.extend(f"{path.name}: {error}" for error in plan_report["errors"])
        warnings.extend(f"{path.name}: {warning}" for warning in plan_report["warnings"])
        if receipt.get("schema_version") != STORE_SCHEMA_VERSION:
            errors.append(f"{path.name}: invalid receipt schema_version")
        receipt_without_hash = dict(receipt)
        observed_receipt_hash = receipt_without_hash.pop("receipt_hash", None)
        if observed_receipt_hash != canonical_hash(receipt_without_hash):
            errors.append(f"{path.name}: receipt integrity hash mismatch")
        if receipt.get("sequence") != sequence:
            errors.append(f"{path.name}: receipt sequence mismatch")
        if receipt.get("plan_id") != plan.get("plan_id"):
            errors.append(f"{path.name}: receipt plan_id mismatch")
        if receipt.get("plan_version") != plan.get("version"):
            errors.append(f"{path.name}: receipt plan_version mismatch")
        if receipt.get("plan_hash") != canonical_hash(plan):
            errors.append(f"{path.name}: plan integrity hash mismatch")

        event_path = path / "event.json"
        if sequence == 1:
            if receipt.get("kind") != "initial" or event_path.exists():
                errors.append(f"{path.name}: sequence 1 must be the initial plan only")
            if plan.get("version") != 1:
                errors.append(f"{path.name}: initial plan version must be 1")
        else:
            if receipt.get("kind") != "decision" or not event_path.is_file():
                errors.append(f"{path.name}: decision commit must contain event.json")
            else:
                try:
                    event = _read_object(event_path)
                except StateStoreError as exc:
                    errors.append(f"{path.name}: {exc}")
                else:
                    event_report = validate_decision_event(event)
                    errors.extend(f"{path.name}: {error}" for error in event_report["errors"])
                    if receipt.get("event_id") != event.get("event_id"):
                        errors.append(f"{path.name}: receipt event_id mismatch")
                    if receipt.get("event_hash") != canonical_hash(event):
                        errors.append(f"{path.name}: event integrity hash mismatch")
                    event_id = event.get("event_id")
                    if isinstance(event_id, str):
                        if event_id in event_index:
                            errors.append(f"{path.name}: duplicate event_id {event_id}")
                        event_index[event_id] = {
                            "event": event,
                            "plan": plan,
                            "receipt": receipt,
                        }
                    if previous_plan is not None:
                        if event.get("plan_id") != previous_plan.get("plan_id"):
                            errors.append(f"{path.name}: event plan_id does not match history")
                        if event.get("plan_version_before") != previous_plan.get("version"):
                            errors.append(f"{path.name}: event before version breaks history")
                        if event.get("plan_version_after") != plan.get("version"):
                            errors.append(f"{path.name}: event after version breaks history")
                        changed = canonical_hash(previous_plan) != canonical_hash(plan)
                        expected_version = previous_plan.get("version", 0) + (1 if changed else 0)
                        if plan.get("version") != expected_version:
                            errors.append(f"{path.name}: PlanState version does not match exact change")
                        # Both deterministic transitions are re-checked on replay, not
                        # only on the write path: a commit that claims one of them is
                        # held to its own narrow fence every time the store is opened.
                        if event.get("reason_codes") == ["delivery_verified"]:
                            errors.extend(
                                f"{path.name}: {error}"
                                for error in _delivery_transition_errors(previous_plan, plan, event)
                            )
                        if event.get("reason_codes") == ["delivery_withdrawn"]:
                            errors.extend(
                                f"{path.name}: {error}"
                                for error in _withdrawal_transition_errors(
                                    previous_plan, plan, event
                                )
                            )
        previous_plan = plan
        current_plan = plan

    if observed_sequences != expected_sequences:
        errors.append("commit sequences must be contiguous from 1")
    if commit_paths:
        last = commit_paths[-1]
        if manifest.get("current_sequence") != observed_sequences[-1]:
            errors.append("store current_sequence does not match immutable history")
        if manifest.get("current_commit") != last.name:
            errors.append("store current_commit does not match immutable history")
        if current_plan is not None and manifest.get("current_version") != current_plan.get("version"):
            errors.append("store current_version does not match current PlanState")
        if current_plan is not None and manifest.get("plan_id") != current_plan.get("plan_id"):
            errors.append("store plan_id does not match current PlanState")

    # The pre-check computed above only ever changes what is reported, never whether
    # doctor found a problem: an older store with no other errors stays "passed" here,
    # exactly as it did before this existed, and goes on to write and snapshot normally.
    if leading_error is not None and errors:
        errors = [leading_error, *errors]

    report = {
        "status": "passed" if not errors else "blocked",
        "errors": errors,
        "warnings": warnings,
        "plan_id": manifest.get("plan_id"),
        "current_version": manifest.get("current_version"),
        "event_count": max(len(commit_paths) - 1, 0),
        "policy": "private_repo_external_current_state",
        "writer_contract_version": (
            observed_contract_version
            if observed_contract_version is not None
            else raw_contract_version
        ),
    }
    return report, current_plan, event_index


def doctor_store(state_dir: Path | str) -> dict[str, Any]:
    report, _, _ = _inspect_store(state_dir)
    try:
        attempt = pending_delivery_attempt(state_dir)
    except StateStoreError as exc:
        # A reservation that cannot be parsed is a blocked store, not a missing one: it
        # may be the only record of an Intervals write, and no maintenance command may
        # run past it. The message carries the exact way out (issue #122).
        report["status"] = "blocked"
        report["errors"] = [*report.get("errors", []), str(exc)]
        report["delivery_attempt_error"] = str(exc)
        return report
    if attempt is not None:
        # Not an error: an open reservation is the normal state of a delivery in flight.
        # It is reported because after an interruption it is the only place the sessions
        # Intervals already accepted are written down.
        report["pending_delivery_attempt"] = attempt
        outstanding = unresolved_delivery_operations(attempt)
        if outstanding:
            # Named separately from the attempt itself, because this is the actionable
            # half: these are the operations Intervals may hold and the plan does not.
            report["unresolved_delivery_operations"] = outstanding
    return report


def _snapshot(root: Path, *, reason: str) -> dict[str, Any]:
    """Copy an already-known-good ``root`` to a verified, timestamped directory beside it.

    Assumes the caller already confirmed ``root`` passes doctor -- this never re-checks
    that itself, so it must never run against a store that has not just been inspected.
    The copy is written under a throwaway name first and only renamed into its final,
    discoverable name once ``doctor_store`` has opened the copy on its own and found it
    clean; nothing a restore could ever be pointed at is left half-written.

    The snapshot lives beside ``root`` (``<name>.snapshots/``), never inside it: ``root``'s
    own contents stay exactly what ``_inspect_store`` already knows how to walk, and
    ``adopt_store``'s copy mode does not pick up snapshots as part of a store it copies.
    """
    manifest = _read_object(root / "store.json")
    snapshots_root = root.parent / f"{root.name}.snapshots"
    snapshots_root.mkdir(parents=True, mode=0o700, exist_ok=True)
    os.chmod(snapshots_root, 0o700)
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    name = f"{stamp}-{_commit_slug(reason)}-{uuid.uuid4().hex[:8]}"
    pending = snapshots_root / f".pending-{uuid.uuid4().hex}"
    try:
        shutil.copytree(root, pending, ignore=_COPY_IGNORE)
        os.chmod(pending, 0o700)
        verification = doctor_store(pending)
        if verification["status"] != "passed":
            raise StateStoreError(
                "snapshot verification failed; nothing was kept", details=verification
            )
        final = snapshots_root / name
        os.replace(pending, final)
    except Exception:
        shutil.rmtree(pending, ignore_errors=True)
        raise
    return {
        "status": "passed",
        "snapshot_dir": str(final),
        "source": str(root),
        "reason": reason,
        "plan_id": manifest.get("plan_id"),
        "writer_contract_version": manifest.get("writer_contract_version", 1),
        "current_version": manifest.get("current_version"),
        "event_count": max(manifest["current_sequence"] - 1, 0),
        "created_at": stamp,
    }


def snapshot_store(state_dir: Path | str, *, reason: str = "manual") -> dict[str, Any]:
    """Take and verify an atomic, point-in-time copy of a store, on demand.

    This is the same mechanism ``apply_decision`` and ``apply_delivery_observations``
    trigger automatically before the first write under a new writer-contract version (see
    ``WRITER_CONTRACT_VERSION``), exposed here so an operator can take one before any
    operation they want an undo for -- and so the restore drill in ``restore_snapshot`` has
    something to test against without waiting for a real contract change.
    """
    root = _state_root(state_dir)
    if not root.is_dir():
        raise StateStoreError("state directory does not exist")
    with _exclusive_lock(root):
        _refuse_while_delivery_in_flight(root, "snapshot-store")
        doctor, _, _ = _inspect_store(root, ignore_lock=True)
        if doctor["status"] != "passed":
            raise StateStoreError(_doctor_failure_message(doctor), details=doctor)
        return _snapshot(root, reason=reason)


def restore_snapshot(
    snapshot_dir: Path | str,
    state_dir: Path | str,
    *,
    confirm: bool = False,
) -> dict[str, Any]:
    """Roll a store back to a previously verified snapshot.

    This is the documented recovery path for the guard in ``apply_decision`` and
    ``apply_delivery_observations``: whatever went wrong after a snapshot was taken, this
    is how an operator gets back to the exact state it captured. ``confirm=False`` runs
    every check and writes nothing -- the same preview/confirm split ``adopt_store`` uses.
    The destination is renamed aside rather than deleted, so a mistaken restore -- wrong
    snapshot, wrong destination -- never destroys the only copy of anything, and a restore
    that fails part way puts the original straight back rather than leaving neither.
    """
    snapshot_root = _state_root(snapshot_dir)
    snapshot_report = doctor_store(snapshot_root)
    if snapshot_report["status"] != "passed":
        raise StateStoreError(
            "snapshot does not open; refusing to restore from it", details=snapshot_report
        )

    destination_root = _state_root(state_dir)
    if (destination_root / ".lock").exists():
        raise StateStoreError("destination state store is locked by another operation")
    # The lock and the reservation answer different questions: `.lock` says a filesystem
    # mutation is running right now, the reservation says a provider transaction is open
    # across network time. Only the second one survives the moment this rename would take
    # (issue #122), and it is checked in preview too -- a preview that promised a restore
    # the confirm would refuse is not a preview of anything.
    _refuse_while_delivery_in_flight(destination_root, "restore-store")

    result = {
        "status": "restored" if confirm else "preview",
        "snapshot_dir": str(snapshot_root),
        "state_dir": str(destination_root),
        "plan_id": snapshot_report["plan_id"],
        "writer_contract_version": snapshot_report["writer_contract_version"],
        "current_version": snapshot_report["current_version"],
        "event_count": snapshot_report["event_count"],
    }
    if not confirm:
        return result

    displaced: Path | None = None
    if destination_root.exists():
        stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        displaced = (
            destination_root.parent
            / f"{destination_root.name}.pre-restore-{stamp}-{uuid.uuid4().hex[:8]}"
        )
        os.replace(destination_root, displaced)
    try:
        shutil.copytree(snapshot_root, destination_root, ignore=_COPY_IGNORE)
        os.chmod(destination_root, 0o700)
        restored_report = doctor_store(destination_root)
        if restored_report["status"] != "passed":
            raise StateStoreError(
                "restored store does not open; nothing was kept", details=restored_report
            )
    except Exception:
        shutil.rmtree(destination_root, ignore_errors=True)
        if displaced is not None:
            os.replace(displaced, destination_root)
        raise
    result["displaced_previous_store"] = str(displaced) if displaced is not None else None
    return result


def delete_owner_store(state_dir: Path | str, *, confirm: bool = False) -> dict[str, Any]:
    """Remove one owner's entire private state, in full or not at all.

    Issue #6's operator deletion, store half. The caller resolves ``state_dir`` through
    ``resolve_state_dir`` first -- its canonical-UUID check is the only thing standing
    between an owner id and someone else's directory, and this function trusts that
    resolution rather than re-deriving it from an owner id itself.

    Deliberately does not resolve ``state_dir`` through ``_state_root`` the way every
    other write path in this module does: that call follows symlinks, and the one shape
    an owner directory can legitimately have is a symlink left by
    ``adopt-owner-store --mode link``, pointing at a store this owner does not
    exclusively own -- most plausibly a CLI operator's own dogfood store. Resolving it
    away and then deleting would delete the target's real data, not this owner's link to
    it. Only the containing directory is resolved (the same "keep the final component
    verbatim" split ``adopt_store`` uses for its own destination), so a symlinked entry is
    still detected as one. Deletion then removes only the link -- the one piece of state
    this owner actually owns -- and leaves the target, and its own history, untouched. A
    real (non-symlink) owner directory, the ordinary case, is removed in full as before.

    Reaches the automatic pre-migration snapshot beside the store (``_snapshot``'s
    ``<name>.snapshots`` sibling, taken by ``apply_decision`` and
    ``apply_delivery_observations`` before a writer-contract upgrade) as well as the store
    itself: a snapshot is a full, verified copy of the same owner's history, and leaving
    it behind would make this "deletion" false. It does not, and cannot, reach anything
    already written to the provider's own calendar -- that boundary is documented, not
    enforced here (the deletion contract's "deleting local product data must not silently claim to
    delete provider-owned history").

    Refuses while a delivery reservation is open, in preview and confirm alike -- the same
    fence ``snapshot_store`` and ``restore_snapshot`` already enforce: an
    unresolved reservation may be the only record of an effect Intervals already holds,
    and deleting the directory that holds it would make that effect both unrecoverable and
    unrecorded in the same stroke. Clear it with ``clear-delivery-attempt``, or finish the
    delivery, before retrying. Not checked for a symlinked owner: removing the link alone
    changes nothing Intervals can observe, so there is nothing to fence.

    ``confirm=False`` previews only; nothing is removed. Idempotent: an owner with neither
    a store directory/link nor a snapshot reports nothing to delete, not an error -- the
    same "unknown resolves to nothing" shape the identity registry's own read paths use.
    """
    raw = Path(state_dir).expanduser()
    if raw.parent == raw:
        raise StateStoreError("state directory must name a directory inside a state root")
    root = _state_root(raw.parent) / raw.name
    is_link = root.is_symlink()
    snapshots_root = root.parent / f"{root.name}.snapshots"
    state_dir_existed = root.exists() or is_link
    snapshots_dir_existed = snapshots_root.exists()
    if not state_dir_existed and not snapshots_dir_existed:
        return {
            "status": "absent",
            "state_dir": str(root),
            "state_dir_existed": False,
            "state_dir_removed": False,
            "state_dir_is_link": False,
            "snapshots_dir": str(snapshots_root),
            "snapshots_dir_existed": False,
            "snapshots_dir_removed": False,
        }
    if state_dir_existed and not is_link:
        _refuse_while_delivery_in_flight(root, "delete-owner")
    if not confirm:
        return {
            "status": "preview",
            "state_dir": str(root),
            "state_dir_existed": state_dir_existed,
            "state_dir_removed": False,
            "state_dir_is_link": is_link,
            "snapshots_dir": str(snapshots_root),
            "snapshots_dir_existed": snapshots_dir_existed,
            "snapshots_dir_removed": False,
        }
    if state_dir_existed:
        if is_link:
            root.unlink()
        else:
            with _exclusive_lock(root):
                shutil.rmtree(root)
    if snapshots_dir_existed:
        shutil.rmtree(snapshots_root)
    return {
        "status": "deleted",
        "state_dir": str(root),
        "state_dir_existed": state_dir_existed,
        "state_dir_removed": state_dir_existed,
        "state_dir_is_link": is_link,
        "snapshots_dir": str(snapshots_root),
        "snapshots_dir_existed": snapshots_dir_existed,
        "snapshots_dir_removed": snapshots_dir_existed,
    }


def _local_today(
    today: dt.date | str | None,
    *,
    timezone: str | None = None,
    now: dt.datetime | None = None,
) -> str:
    """The athlete's own calendar date, as an ISO string.

    "Next" is a question about a calendar, so it needs the athlete's calendar. UTC would
    answer with yesterday through the first eight hours of every Taipei morning -- which
    is when the day's session is decided. An unresolvable zone is refused rather than
    quietly answered in the wrong one.

    ``today`` is an explicit override and wins outright: an already-resolved date needs
    no timezone to interpret it, so ``timezone`` is never even consulted when ``today``
    is given. Only when no date is given does an athlete's own IANA ``timezone`` (issue
    #112) decide what "today" means; omitting it too keeps every existing owner's
    ``DEFAULT_TIMEZONE`` (Asia/Taipei) behavior unchanged. ``now`` is an optional
    injection point for deterministic tests, mirroring
    ``context_builder.build_context``'s own ``now`` parameter -- it must be
    timezone-aware when given, and defaults to the real wall clock.
    """
    if isinstance(today, dt.date):
        return today.isoformat()
    if isinstance(today, str) and today:
        return dt.date.fromisoformat(today).isoformat()
    zone_name = timezone or DEFAULT_TIMEZONE
    try:
        zone = ZoneInfo(zone_name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise StateStoreError(f"unknown timezone: {zone_name!r}") from exc
    moment = now if now is not None else dt.datetime.now(dt.timezone.utc)
    return moment.astimezone(zone).date().isoformat()


def status_store(
    state_dir: Path | str,
    *,
    today: dt.date | str | None = None,
    timezone: str | None = None,
    now: dt.datetime | None = None,
) -> dict[str, Any]:
    """Store health, what to do next, and the current plan.

    ``next_session`` answers "what comes next", so it never looks backwards: a session
    whose day has passed cannot be next, whatever its recorded outcome. Sessions that did
    pass without one are reported as ``elapsed_without_outcome`` -- a plain fact, not a
    question. Not training is a normal state; whether a particular missed session matters
    enough to reshape the week is the coach's judgment, and re-planning is where it gets
    made (AGENTS.md 4).

    Every actionable status counts, not only ``planned``: a session that was moved or
    replaced is still work the athlete is meant to do, and dropping it from "next" hid
    exactly the session a plan change had just touched.

    ``timezone`` (issue #112) lets a caller answer from an explicit IANA zone instead of
    pre-computing ``today`` -- the same athlete-local resolution every context-building
    command already applies to ``as_of`` via ``ContextRequest.timezone_name``, so this and
    ``build-context``/``refresh-context`` can never disagree about which calendar day it
    is around a UTC/local midnight boundary. See ``_local_today`` for how ``today``,
    ``timezone`` and ``now`` combine.
    """
    report, plan, _ = _inspect_store(state_dir)
    if report["status"] != "passed" or plan is None:
        raise StateStoreError(_doctor_failure_message(report), details=report)
    as_of = _local_today(today, timezone=timezone, now=now)
    actionable = sorted(
        (
            session for session in plan.get("week", {}).get("sessions", [])
            if session.get("match_status") in ACTIONABLE_MATCH_STATUSES
        ),
        key=lambda session: (session.get("scheduled_date", ""), session.get("session_id", "")),
    )
    upcoming = [s for s in actionable if s.get("scheduled_date", "") >= as_of]
    status = {
        **report,
        "as_of_date": as_of,
        "next_session": upcoming[0] if upcoming else None,
        "elapsed_without_outcome": [s for s in actionable if s.get("scheduled_date", "") < as_of],
        "current_plan": plan,
    }
    # Deliberately not caught: an unreadable reservation blocks `status` exactly as it
    # blocks every other entry point, and the exception carries the way out.
    attempt = pending_delivery_attempt(state_dir)
    if attempt is not None:
        status["pending_delivery_attempt"] = attempt
        outstanding = unresolved_delivery_operations(attempt)
        if outstanding:
            status["unresolved_delivery_operations"] = outstanding
    return status


def read_current_plan(state_dir: Path | str) -> dict[str, Any]:
    """Read the current PlanState through the manifest pointer, verifying only its hashes.

    ``status_store`` answers the same question by replaying and revalidating every commit.
    That is the right cost for a session that starts by asking whether the whole history
    is sound, and the wrong cost for a request path that serves many reads against one
    plan. This opens exactly the two files the manifest points at and checks the receipt's
    own integrity hash plus the plan hash it records, so a tampered or torn *current*
    commit still fails closed.

    It is not a doctor: it says nothing about earlier commits. Every write path keeps
    running the full check under the store lock, so nothing new can be appended on top of
    a broken chain.
    """
    root = _state_root(state_dir)
    if not root.is_dir():
        raise StateStoreError("state directory does not exist")
    if (root / ".lock").exists():
        raise StateStoreError("state store is locked by another operation")
    manifest = _read_object(root / "store.json")
    if manifest.get("schema_version") != STORE_SCHEMA_VERSION:
        raise StateStoreError(f"store schema_version must be {STORE_SCHEMA_VERSION}")
    commit_name = manifest.get("current_commit")
    if not isinstance(commit_name, str) or not COMMIT_PATTERN.match(commit_name):
        raise StateStoreError("store.json does not name a current commit")
    commit = root / "commits" / commit_name
    plan = _read_object(commit / "plan.json")
    receipt = _read_object(commit / "receipt.json")
    receipt_material = dict(receipt)
    if receipt_material.pop("receipt_hash", None) != canonical_hash(receipt_material):
        raise StateStoreError(f"{commit_name}: receipt integrity hash mismatch")
    if receipt.get("plan_hash") != canonical_hash(plan):
        raise StateStoreError(f"{commit_name}: plan integrity hash mismatch")
    if manifest.get("plan_id") != plan.get("plan_id"):
        raise StateStoreError("store plan_id does not match the current PlanState")
    if manifest.get("current_version") != plan.get("version"):
        raise StateStoreError("store current_version does not match the current PlanState")
    sequence = manifest.get("current_sequence")
    return {
        "status": "passed",
        "plan_id": plan["plan_id"],
        "current_version": plan["version"],
        "current_commit": commit_name,
        "event_count": max(sequence - 1, 0) if isinstance(sequence, int) else 0,
        "current_plan": plan,
        "receipt": receipt,
        "policy": "current_commit_only",
    }


def _sessions_by_id(plan: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        session["session_id"]: session
        for session in plan.get("week", {}).get("sessions", [])
        if isinstance(session, dict) and isinstance(session.get("session_id"), str)
    }


def _changed_fields(before: dict[str, Any], after: dict[str, Any]) -> list[str]:
    return sorted(
        key for key in set(before) | set(after)
        if json.dumps(before.get(key), sort_keys=True, ensure_ascii=False)
        != json.dumps(after.get(key), sort_keys=True, ensure_ascii=False)
    )


def plan_diff(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    """The exact material difference between two PlanState versions.

    ``history_store`` derives this per stored revision; a caller previewing a *candidate*
    PlanState needs the same answer before anything is committed, so the derivation lives
    here once instead of in each caller.
    """
    previous = _sessions_by_id(before)
    current = _sessions_by_id(after)
    before_week = before.get("week") if isinstance(before.get("week"), dict) else {}
    after_week = after.get("week") if isinstance(after.get("week"), dict) else {}
    return {
        "sessions_added": sorted(set(current) - set(previous)),
        "sessions_removed": sorted(set(previous) - set(current)),
        "sessions_modified": [
            {"session_id": sid, "fields": _changed_fields(previous[sid], current[sid])}
            for sid in sorted(set(current) & set(previous))
            if _changed_fields(previous[sid], current[sid])
        ],
        "week_changed_fields": [
            field for field in _changed_fields(before_week, after_week) if field != "sessions"
        ],
        "goal_changed": before.get("goal") != after.get("goal"),
        "cycle_changed": before.get("cycle") != after.get("cycle"),
        "baseline_changed": before.get("athlete_baseline") != after.get("athlete_baseline"),
    }


def cycle_sessions(state_dir: Path | str, *, since: str, before: str) -> list[dict[str, Any]]:
    """Every session scheduled in ``[since, before)``, in the last state it was written with.

    The plan holds one week; every earlier week exists only in the commit chain, where a
    session that rolled out of the week keeps the last state it was written with. Reading
    that chain is what makes "planned five, did three" answerable at all -- without it the
    record resets every Monday and there is no such thing as *too many* missed sessions.

    Completed work stays in: this is the record of what the cycle prescribed, not a list
    of failures. "Planned but not done" is one reading of it -- a session with no attached
    activity whose status never reached completed -- and the two readings a coach needs
    ("could not finish it" versus "never started it") are the same rows read against what
    came back, not two separately maintained lists.

    Three things are deliberately not in the record:

    - a session the plan **rewrote out of the week it was still in**. Dropping it was the
      plan's own change of mind, made while the day was still ahead, so the athlete was
      never expected to do it -- counting that as their miss would report the coach's
      decisions as the athlete's. A session that instead survived until the week rolled
      past it was live on its day, and stays in the record. The week boundary answers
      this and the clock is not consulted: the version that dropped the session either
      still covered its date, or had already moved beyond it;
    - a rescheduled session, twice. It is identified by ``session_id`` across versions, so
      one piece of work that moved counts once;
    - rest days, which are not work.
    """
    root = _state_root(state_dir)
    commits = sorted(
        path for path in (root / "commits").iterdir()
        if path.is_dir() and COMMIT_PATTERN.match(path.name)
    )
    # session_id -> (week start of the version that dropped it, or None while carried)
    latest: dict[str, tuple[str | None, dict[str, Any]]] = {}
    for commit in commits:
        plan_path = commit / "plan.json"
        if not plan_path.exists():
            continue
        plan = _read_object(plan_path)
        carried = _sessions_by_id(plan)
        week_start = (plan.get("week") or {}).get("start")
        for session_id, (dropped_by_week, session) in list(latest.items()):
            if session_id not in carried and dropped_by_week is None:
                latest[session_id] = (week_start, session)
        for session in carried.values():
            latest[session["session_id"]] = (None, session)
    return sorted(
        (
            session
            for dropped_by_week, session in latest.values()
            if session.get("sport") != "rest"
            and isinstance(session.get("scheduled_date"), str)
            and since <= session["scheduled_date"] < before
            and (
                dropped_by_week is None
                or not isinstance(dropped_by_week, str)
                or session["scheduled_date"] < dropped_by_week
            )
        ),
        key=lambda session: (session["scheduled_date"], session["session_id"]),
    )


def history_store(state_dir: Path | str, *, session_id: str | None = None) -> dict[str, Any]:
    """Replay the commit chain into a readable revision history.

    The store already holds every version; what it does not hold is an answer to
    "when did this session change, and what pushed it". This walks the chain and
    derives that per revision -- and, with ``session_id``, follows one session
    across its whole life so a prescription that drifted over four versions is
    visible without diffing plan snapshots by hand.

    ``initiative`` is surfaced whether or not the events carry it yet: a null
    column is an honest "we never recorded this", not a silent omission.
    """
    report, _, _ = _inspect_store(state_dir)
    if report["status"] != "passed":
        raise StateStoreError(_doctor_failure_message(report), details=report)

    root = _state_root(state_dir)
    commits = sorted(
        path for path in (root / "commits").iterdir()
        if path.is_dir() and COMMIT_PATTERN.match(path.name)
    )

    revisions: list[dict[str, Any]] = []
    timeline: list[dict[str, Any]] = []
    previous_plan: dict[str, Any] | None = None

    for commit in commits:
        plan = _read_object(commit / "plan.json")
        event_path = commit / "event.json"
        event = _read_object(event_path) if event_path.exists() else {}
        current = _sessions_by_id(plan)
        previous = _sessions_by_id(previous_plan) if previous_plan else {}
        diff = plan_diff(previous_plan or {}, plan)

        revisions.append({
            "version": plan.get("version"),
            "commit": commit.name,
            "event_id": event.get("event_id"),
            "mode": event.get("mode"),
            "action": event.get("action"),
            # Null until the DecisionEvent carries it -- an honest gap, not an omission.
            "initiative": event.get("initiative"),
            "trigger": event.get("trigger"),
            "authored_by": event.get("authored_by"),
            "supersedes": event.get("supersedes"),
            "reason_codes": event.get("reason_codes", []),
            "created_at": event.get("created_at"),
            "summary": (event.get("change") or {}).get("summary"),
            "sessions_added": diff["sessions_added"],
            "sessions_removed": diff["sessions_removed"],
            "sessions_modified": diff["sessions_modified"],
            # The first revision has nothing to differ from; "changed" there would claim
            # a revision that never happened.
            "cycle_changed": bool(previous_plan) and diff["cycle_changed"],
            "baseline_changed": bool(previous_plan) and diff["baseline_changed"],
        })

        if session_id is not None:
            entry: dict[str, Any] | None = None
            if session_id in current and session_id not in previous:
                entry = {"change": "created", "fields": []}
            elif session_id in previous and session_id not in current:
                entry = {"change": "removed", "fields": []}
            elif session_id in current and session_id in previous:
                fields = _changed_fields(previous[session_id], current[session_id])
                if fields:
                    entry = {"change": "modified", "fields": fields}
            if entry is not None:
                timeline.append({
                    "version": plan.get("version"),
                    "event_id": event.get("event_id"),
                    "initiative": event.get("initiative"),
                    "authored_by": event.get("authored_by"),
                    "supersedes": event.get("supersedes"),
                    "reason_codes": event.get("reason_codes", []),
                    "summary": (event.get("change") or {}).get("summary"),
                    **entry,
                    "snapshot": current.get(session_id),
                })

        previous_plan = plan

    result: dict[str, Any] = {
        # Every command reports a status; history is only ever reached with a
        # passing doctor, but the caller should not have to know that.
        "status": report["status"],
        "plan_id": report.get("plan_id"),
        "current_version": report.get("current_version"),
        "revision_count": len(revisions),
        "revisions": revisions,
    }
    if session_id is not None:
        result["session_id"] = session_id
        result["timeline"] = timeline
        if not timeline:
            result["note"] = f"no revision of this plan has ever contained session {session_id}"
    return result


def apply_decision(
    state_dir: Path | str,
    *,
    context: dict[str, Any],
    after: dict[str, Any],
    event: dict[str, Any],
) -> dict[str, Any]:
    root = _state_root(state_dir)
    if not root.is_dir():
        raise StateStoreError("state directory does not exist; run init-store first")
    snapshot: dict[str, Any] | None = None
    with _exclusive_lock(root):
        doctor, before, event_index = _inspect_store(root, ignore_lock=True)
        if doctor["status"] != "passed" or before is None:
            raise StateStoreError(_doctor_failure_message(doctor), details=doctor)
        # A plan revision committed while an approved delivery is writing to Intervals
        # would leave the athlete's calendar holding content no PlanState version ever
        # described (issue #110). The delivery either finishes or is cleared first.
        in_flight = _read_delivery_attempt(root)
        if in_flight is not None:
            raise StateStoreError(
                _delivery_attempt_conflict_message(in_flight), details=in_flight
            )
        event_id = event.get("event_id")
        if not isinstance(event_id, str) or not event_id:
            raise StateStoreError("DecisionEvent event_id is required")
        context_hash = canonical_hash(context)
        existing = event_index.get(event_id)
        if existing is not None:
            if (
                existing["receipt"].get("context_hash") == context_hash
                and existing["receipt"].get("event_hash") == canonical_hash(event)
                and existing["receipt"].get("plan_hash") == canonical_hash(after)
            ):
                return {
                    "status": "passed",
                    "idempotent_replay": True,
                    "plan_id": before["plan_id"],
                    "current_version": before["version"],
                    "event_count": doctor["event_count"],
                    "validation": {"status": "passed", "errors": [], "warnings": []},
                    "policy": "private_repo_external_current_state",
                }
            raise StateStoreError("event_id already exists with different content")

        if event.get("plan_version_before") != before.get("version"):
            raise StateStoreError("DecisionEvent before version is not the current PlanState")
        validation = validate_bundle(context, before, after, event)
        if validation["status"] != "passed":
            raise StateStoreError("decision bundle failed validation", details=validation)
        changed = canonical_hash(before) != canonical_hash(after)
        expected_version = before["version"] + (1 if changed else 0)
        if after.get("version") != expected_version:
            raise StateStoreError("PlanState version does not match exact change")

        # Every check above has passed, so the write below is certain to happen -- the
        # first (and only) point a snapshot is worth taking. Any earlier and an idempotent
        # replay or a validation failure, neither of which writes anything, would trigger
        # one for nothing.
        if doctor["writer_contract_version"] < WRITER_CONTRACT_VERSION:
            snapshot = _snapshot(
                root,
                reason=f"writer-contract-{doctor['writer_contract_version']}-to-{WRITER_CONTRACT_VERSION}",
            )

        manifest = _read_object(root / "store.json")
        sequence = manifest["current_sequence"] + 1
        commit_name, receipt = _write_commit(
            root,
            sequence=sequence,
            plan=after,
            event=event,
            context_hash=context_hash,
        )
        updated_manifest = _manifest(
            plan_id=after["plan_id"],
            sequence=sequence,
            version=after["version"],
            commit_name=commit_name,
            created_at=manifest["created_at"],
            updated_at=receipt["created_at"],
        )
        _atomic_json(root / "store.json", updated_manifest)
    result = {
        "status": "passed",
        "idempotent_replay": False,
        "plan_id": after["plan_id"],
        "current_version": after["version"],
        "event_count": sequence - 1,
        "validation": validation,
        "policy": "private_repo_external_current_state",
    }
    if snapshot is not None:
        result["writer_contract_snapshot"] = snapshot["snapshot_dir"]
    return result


def apply_delivery_observations(
    state_dir: Path | str,
    *,
    observations: list[dict[str, Any]],
    attempt_id: str | None = None,
) -> dict[str, Any]:
    """Atomically append one verified Intervals delivery set to current state.

    ``attempt_id`` is the reservation this delivery opened before its first provider
    write. Passing it is what distinguishes "the delivery that is in flight is now
    recording its own result" from "something else is trying to change state while a
    delivery is in flight", which is refused.
    """
    required = {
        "plan_id",
        "plan_version",
        "session_id",
        "session_content_hash",
        "external_id",
        "proposal_hash",
        "readback_hash",
        "verified_at",
    }
    if not isinstance(observations, list) or not observations:
        raise StateStoreError("delivery observations must contain at least one verified item")
    for index, observation in enumerate(observations):
        if not isinstance(observation, dict) or set(observation) != required:
            observed = set(observation) if isinstance(observation, dict) else set()
            raise StateStoreError(
                f"delivery observation {index} fields do not match the exact contract",
                details={
                    "missing": sorted(required - observed),
                    "extra": sorted(observed - required),
                },
            )
        for field in required - {"plan_version"}:
            if not isinstance(observation.get(field), str) or not observation[field].strip():
                raise StateStoreError(
                    f"delivery observation {index} {field} must be a non-empty string"
                )
        version = observation.get("plan_version")
        if isinstance(version, bool) or not isinstance(version, int) or version < 1:
            raise StateStoreError(
                f"delivery observation {index} plan_version must be a positive integer"
            )
    ordered_observations = sorted(observations, key=lambda item: item["session_id"])
    session_ids = [observation["session_id"] for observation in ordered_observations]
    external_ids = [observation["external_id"] for observation in ordered_observations]
    if len(session_ids) != len(set(session_ids)):
        raise StateStoreError("delivery observations must bind unique session_id values")
    if len(external_ids) != len(set(external_ids)):
        raise StateStoreError("delivery observations must bind unique Intervals event ids")
    plan_versions = {observation["plan_version"] for observation in ordered_observations}
    if len(plan_versions) != 1:
        raise StateStoreError("delivery observations must bind one exact PlanState version")

    observation_context_hash = canonical_hash(
        {"delivery_observations": ordered_observations}
    )

    root = _state_root(state_dir)
    if not root.is_dir():
        raise StateStoreError("state directory does not exist; run init-store first")
    snapshot: dict[str, Any] | None = None
    with _exclusive_lock(root):
        doctor, before, event_index = _inspect_store(root, ignore_lock=True)
        if doctor["status"] != "passed" or before is None:
            raise StateStoreError(_doctor_failure_message(doctor), details=doctor)
        in_flight = _read_delivery_attempt(root)
        if in_flight is not None and in_flight.get("attempt_id") != attempt_id:
            raise StateStoreError(
                _delivery_attempt_conflict_message(in_flight), details=in_flight
            )

        existing_delivery = next(
            (
                item
                for item in event_index.values()
                if item["event"].get("reason_codes") == ["delivery_verified"]
                and item["receipt"].get("context_hash") == observation_context_hash
            ),
            None,
        )
        if existing_delivery is not None:
            recorded_sessions = _sessions_by_id(existing_delivery["plan"])
            recorded_matches = True
            for observation in ordered_observations:
                recorded_session = recorded_sessions.get(observation["session_id"]) or {}
                recorded_execution = recorded_session.get("execution") or {}
                if (
                    recorded_execution.get("delivery_state") != "intervals_accepted"
                    or recorded_execution.get("external_id") != observation["external_id"]
                ):
                    recorded_matches = False
                    break
            if recorded_matches:
                return {
                    "status": "passed",
                    "idempotent_replay": True,
                    "plan_id": before["plan_id"],
                    "current_version": before["version"],
                    "event_count": doctor["event_count"],
                    "session_ids": session_ids,
                    "delivery_state": "intervals_accepted",
                    "external_ids": external_ids,
                    "policy": "verified_intervals_delivery",
                }
            raise StateStoreError("delivery receipt already exists with conflicting state")

        if any(
            observation["plan_id"] != before.get("plan_id")
            for observation in ordered_observations
        ):
            raise StateStoreError("delivery observation plan_id is not the current plan")
        expected_version = next(iter(plan_versions))
        if before.get("version") != expected_version:
            raise StateStoreError(
                "delivery observation PlanState version is stale; refusing state advancement"
            )

        sessions = _sessions_by_id(before)
        pending: list[dict[str, Any]] = []
        for observation in ordered_observations:
            session = sessions.get(observation["session_id"])
            if session is None:
                raise StateStoreError("delivery observation session_id is not in the current week")
            if delivery_session_content_hash(session) != observation["session_content_hash"]:
                raise StateStoreError(
                    f"session {observation['session_id']} changed after delivery verification; "
                    "refusing state advancement"
                )
            execution = (
                session.get("execution") if isinstance(session.get("execution"), dict) else {}
            )
            if (
                execution.get("delivery_state") != "not_published"
                or execution.get("external_id") is not None
            ):
                raise StateStoreError(
                    f"session {observation['session_id']} delivery state conflicts with the verified observation"
                )
            pending.append(observation)

        after = copy.deepcopy(before)
        after_sessions = _sessions_by_id(after)
        for observation in pending:
            after_session = after_sessions[observation["session_id"]]
            after_session["execution"]["external_id"] = observation["external_id"]
            after_session["execution"]["delivery_state"] = "intervals_accepted"
            if (
                after_session["execution"].get("superseded_external_id")
                == observation["external_id"]
            ):
                after_session["execution"].pop("superseded_external_id")
        after["version"] = before["version"] + 1
        event_identity = {
            "plan_id": before["plan_id"],
            "deliveries": [
                {
                    "session_id": observation["session_id"],
                    "external_id": observation["external_id"],
                    "proposal_hash": observation["proposal_hash"],
                    "session_content_hash": observation["session_content_hash"],
                }
                for observation in pending
            ],
        }
        changed_session_ids = [observation["session_id"] for observation in pending]
        event = {
            "schema_version": "1.0",
            "event_id": f"delivery-{canonical_hash(event_identity)[:24]}",
            "mode": "record_delivery",
            "plan_id": before["plan_id"],
            "plan_version_before": before["version"],
            "plan_version_after": after["version"],
            "action": "record",
            "session_id": changed_session_ids[0] if len(changed_session_ids) == 1 else None,
            "inputs_used": [
                "approved delivery proposal",
                "Intervals.icu event read-back",
            ],
            "evidence": [
                item
                for observation in pending
                for item in (
                    {
                        "field": f"week.sessions.{observation['session_id']}.execution.external_id",
                        "observation": observation["external_id"],
                    },
                    {
                        "field": f"delivery.{observation['session_id']}.readback_hash",
                        "observation": observation["readback_hash"],
                    },
                    {
                        "field": f"delivery.{observation['session_id']}.proposal_hash",
                        "observation": observation["proposal_hash"],
                    },
                )
            ],
            "unknowns": [],
            "reason_codes": ["delivery_verified"],
            "change": {
                "before": "No verified Intervals delivery was recorded.",
                "after": "Intervals accepted every workout and each exact read-back matched.",
                "summary": f"Recorded verified delivery for {', '.join(changed_session_ids)}.",
            },
            "goal_effect": {
                "week": "Training prescription unchanged; delivery is now observable.",
                "cycle": "No effect on the 28-day direction.",
            },
            "next_review_condition": "Revisit the session after its actual activity is available.",
            "created_at": max(observation["verified_at"] for observation in pending),
        }
        plan_report = validate_plan_state(after)
        event_report = validate_decision_event(event)
        transition_errors = _delivery_transition_errors(before, after, event)
        validation = {
            "status": "passed" if not (plan_report["errors"] or event_report["errors"] or transition_errors) else "blocked",
            "errors": [
                *(f"after: {error}" for error in plan_report["errors"]),
                *(f"event: {error}" for error in event_report["errors"]),
                *transition_errors,
            ],
            "warnings": [
                *(f"after: {warning}" for warning in plan_report["warnings"]),
                *(f"event: {warning}" for warning in event_report["warnings"]),
            ],
        }
        if validation["status"] != "passed":
            raise StateStoreError("delivery observation failed validation", details=validation)

        # See the identical comment in apply_decision: everything above has to pass
        # before a write is certain, so this is the one place to take it.
        if doctor["writer_contract_version"] < WRITER_CONTRACT_VERSION:
            snapshot = _snapshot(
                root,
                reason=f"writer-contract-{doctor['writer_contract_version']}-to-{WRITER_CONTRACT_VERSION}",
            )

        manifest = _read_object(root / "store.json")
        sequence = manifest["current_sequence"] + 1
        commit_name, receipt = _write_commit(
            root,
            sequence=sequence,
            plan=after,
            event=event,
            context_hash=observation_context_hash,
        )
        updated_manifest = _manifest(
            plan_id=after["plan_id"],
            sequence=sequence,
            version=after["version"],
            commit_name=commit_name,
            created_at=manifest["created_at"],
            updated_at=receipt["created_at"],
        )
        _atomic_json(root / "store.json", updated_manifest)
    result = {
        "status": "passed",
        "idempotent_replay": False,
        "plan_id": after["plan_id"],
        "current_version": after["version"],
        "event_count": sequence - 1,
        "event_id": event["event_id"],
        "session_ids": changed_session_ids,
        "delivery_state": "intervals_accepted",
        "external_ids": [observation["external_id"] for observation in pending],
        "validation": validation,
        "policy": "verified_intervals_delivery",
    }
    if snapshot is not None:
        result["writer_contract_snapshot"] = snapshot["snapshot_dir"]
    return result


def _withdrawal_transition_errors(
    before: dict[str, Any],
    after: dict[str, Any],
    event: dict[str, Any],
) -> list[str]:
    """Fence the one mutation a verified withdrawal may record.

    A withdrawal removes a provider event the plan had already stopped describing. The
    only thing it may change in PlanState is the record of that outstanding event; it
    cannot touch delivery state, the plan, or anything else. Nothing here writes to
    Intervals: by the time this runs, the event is already verified gone.
    """
    errors: list[str] = []
    if event.get("mode") != "record_delivery" or event.get("action") != "record":
        errors.append("withdrawal event requires mode record_delivery and action record")
    if event.get("reason_codes") != ["delivery_withdrawn"]:
        errors.append("withdrawal event requires only reason code delivery_withdrawn")
    if before.get("plan_id") != after.get("plan_id") or event.get("plan_id") != before.get("plan_id"):
        errors.append("withdrawal event must preserve and bind plan_id")
    if event.get("plan_version_before") != before.get("version"):
        errors.append("withdrawal event before version mismatch")
    if event.get("plan_version_after") != after.get("version"):
        errors.append("withdrawal event after version mismatch")
    if after.get("version") != before.get("version", 0) + 1:
        errors.append("withdrawal event must increment PlanState version exactly once")

    for field in ("schema_version", "status", "goal", "cycle", "athlete_baseline"):
        if before.get(field) != after.get(field):
            errors.append(f"withdrawal event must not change {field}")
    before_sessions = _sessions_by_id(before)
    after_sessions = _sessions_by_id(after)
    if set(before_sessions) != set(after_sessions):
        errors.append("withdrawal event must preserve the weekly session set")
        return errors
    changed = [
        candidate
        for candidate in before_sessions
        if before_sessions[candidate] != after_sessions[candidate]
    ]
    if not changed:
        errors.append("withdrawal event must change at least one session")
        return errors
    for changed_id in changed:
        before_execution = before_sessions[changed_id].get("execution") or {}
        after_execution = after_sessions[changed_id].get("execution") or {}
        for field in set(before_sessions[changed_id]) | set(after_sessions[changed_id]):
            if field != "execution" and before_sessions[changed_id].get(field) != after_sessions[
                changed_id
            ].get(field):
                errors.append(f"withdrawal event must not change session field {field}")
        if not before_execution.get("superseded_external_id"):
            errors.append(
                f"withdrawal for {changed_id} must start from a recorded superseded event"
            )
        expected_execution = {
            key: value
            for key, value in before_execution.items()
            if key != "superseded_external_id"
        }
        if after_execution != expected_execution:
            errors.append(
                f"withdrawal event may only clear superseded_external_id for {changed_id}"
            )
    return errors


def apply_delivery_withdrawals(
    state_dir: Path | str,
    *,
    withdrawals: list[dict[str, Any]],
    attempt_id: str | None = None,
) -> dict[str, Any]:
    """Record that a superseded Intervals event is verified gone from the calendar."""
    required = {
        "plan_id",
        "plan_version",
        "session_id",
        "withdrawn_external_id",
        "owned_external_id",
        "verified_at",
    }
    if not isinstance(withdrawals, list) or not withdrawals:
        raise StateStoreError("delivery withdrawals must contain at least one verified item")
    for index, withdrawal in enumerate(withdrawals):
        if not isinstance(withdrawal, dict) or set(withdrawal) != required:
            observed = set(withdrawal) if isinstance(withdrawal, dict) else set()
            raise StateStoreError(
                f"delivery withdrawal {index} fields do not match the exact contract",
                details={
                    "missing": sorted(required - observed),
                    "extra": sorted(observed - required),
                },
            )
        for field in required - {"plan_version"}:
            if not isinstance(withdrawal.get(field), str) or not withdrawal[field].strip():
                raise StateStoreError(
                    f"delivery withdrawal {index} {field} must be a non-empty string"
                )
        version = withdrawal.get("plan_version")
        if isinstance(version, bool) or not isinstance(version, int) or version < 1:
            raise StateStoreError(
                f"delivery withdrawal {index} plan_version must be a positive integer"
            )
    ordered = sorted(withdrawals, key=lambda item: item["session_id"])
    session_ids = [withdrawal["session_id"] for withdrawal in ordered]
    if len(session_ids) != len(set(session_ids)):
        raise StateStoreError("delivery withdrawals must bind unique session_id values")
    if len({withdrawal["plan_version"] for withdrawal in ordered}) != 1:
        raise StateStoreError("delivery withdrawals must bind one exact PlanState version")
    withdrawal_context_hash = canonical_hash({"delivery_withdrawals": ordered})

    root = _state_root(state_dir)
    if not root.is_dir():
        raise StateStoreError("state directory does not exist; run init-store first")
    snapshot: dict[str, Any] | None = None
    with _exclusive_lock(root):
        doctor, before, event_index = _inspect_store(root, ignore_lock=True)
        if doctor["status"] != "passed" or before is None:
            raise StateStoreError(_doctor_failure_message(doctor), details=doctor)
        in_flight = _read_delivery_attempt(root)
        if in_flight is not None and in_flight.get("attempt_id") != attempt_id:
            raise StateStoreError(
                _delivery_attempt_conflict_message(in_flight), details=in_flight
            )
        existing = next(
            (
                item
                for item in event_index.values()
                if item["event"].get("reason_codes") == ["delivery_withdrawn"]
                and item["receipt"].get("context_hash") == withdrawal_context_hash
            ),
            None,
        )
        if existing is not None:
            return {
                "status": "passed",
                "idempotent_replay": True,
                "plan_id": before["plan_id"],
                "current_version": before["version"],
                "event_count": doctor["event_count"],
                "session_ids": session_ids,
                "withdrawn_external_ids": [
                    withdrawal["withdrawn_external_id"] for withdrawal in ordered
                ],
                "policy": "verified_intervals_withdrawal",
            }
        if any(withdrawal["plan_id"] != before.get("plan_id") for withdrawal in ordered):
            raise StateStoreError("delivery withdrawal plan_id is not the current plan")
        if before.get("version") != ordered[0]["plan_version"]:
            raise StateStoreError(
                "delivery withdrawal PlanState version is stale; refusing state advancement"
            )

        sessions = _sessions_by_id(before)
        for withdrawal in ordered:
            session = sessions.get(withdrawal["session_id"])
            if session is None:
                raise StateStoreError("delivery withdrawal session_id is not in the current week")
            execution = session.get("execution") if isinstance(session.get("execution"), dict) else {}
            if execution.get("superseded_external_id") != withdrawal["withdrawn_external_id"]:
                raise StateStoreError(
                    f"session {withdrawal['session_id']} does not hold "
                    f"{withdrawal['withdrawn_external_id']} as a superseded event"
                )

        after = copy.deepcopy(before)
        after_sessions = _sessions_by_id(after)
        for withdrawal in ordered:
            after_sessions[withdrawal["session_id"]]["execution"].pop(
                "superseded_external_id", None
            )
        after["version"] = before["version"] + 1
        event = {
            "schema_version": "1.0",
            "event_id": "withdrawal-" + canonical_hash(
                {
                    "plan_id": before["plan_id"],
                    "withdrawals": [
                        {
                            "session_id": withdrawal["session_id"],
                            "withdrawn_external_id": withdrawal["withdrawn_external_id"],
                        }
                        for withdrawal in ordered
                    ],
                }
            )[:24],
            "mode": "record_delivery",
            "plan_id": before["plan_id"],
            "plan_version_before": before["version"],
            "plan_version_after": after["version"],
            "action": "record",
            "session_id": session_ids[0] if len(session_ids) == 1 else None,
            "inputs_used": [
                "confirmed withdrawal proposal",
                "Intervals.icu event absence read-back",
            ],
            "evidence": [
                {
                    "field": f"delivery.{withdrawal['session_id']}.withdrawn_external_id",
                    "observation": withdrawal["withdrawn_external_id"],
                }
                for withdrawal in ordered
            ],
            "unknowns": [],
            "reason_codes": ["delivery_withdrawn"],
            "change": {
                "before": "A superseded Intervals event was still on the calendar.",
                "after": "The superseded event is gone and the calendar matches the plan.",
                "summary": f"Withdrew the superseded delivery for {', '.join(session_ids)}.",
            },
            "goal_effect": {
                "week": "Training prescription unchanged; the calendar now matches it.",
                "cycle": "No effect on the 28-day direction.",
            },
            "next_review_condition": "Deliver the current content for this session when it is ready.",
            "created_at": max(withdrawal["verified_at"] for withdrawal in ordered),
        }
        plan_report = validate_plan_state(after)
        event_report = validate_decision_event(event)
        transition_errors = _withdrawal_transition_errors(before, after, event)
        validation = {
            "status": "passed"
            if not (plan_report["errors"] or event_report["errors"] or transition_errors)
            else "blocked",
            "errors": [
                *(f"after: {error}" for error in plan_report["errors"]),
                *(f"event: {error}" for error in event_report["errors"]),
                *transition_errors,
            ],
            "warnings": [
                *(f"after: {warning}" for warning in plan_report["warnings"]),
                *(f"event: {warning}" for warning in event_report["warnings"]),
            ],
        }
        if validation["status"] != "passed":
            raise StateStoreError("delivery withdrawal failed validation", details=validation)

        if doctor["writer_contract_version"] < WRITER_CONTRACT_VERSION:
            snapshot = _snapshot(
                root,
                reason=f"writer-contract-{doctor['writer_contract_version']}-to-{WRITER_CONTRACT_VERSION}",
            )
        manifest = _read_object(root / "store.json")
        sequence = manifest["current_sequence"] + 1
        commit_name, receipt = _write_commit(
            root,
            sequence=sequence,
            plan=after,
            event=event,
            context_hash=withdrawal_context_hash,
        )
        _atomic_json(
            root / "store.json",
            _manifest(
                plan_id=after["plan_id"],
                sequence=sequence,
                version=after["version"],
                commit_name=commit_name,
                created_at=manifest["created_at"],
                updated_at=receipt["created_at"],
            ),
        )
    result = {
        "status": "passed",
        "idempotent_replay": False,
        "plan_id": after["plan_id"],
        "current_version": after["version"],
        "event_count": sequence - 1,
        "event_id": event["event_id"],
        "session_ids": session_ids,
        "withdrawn_external_ids": [
            withdrawal["withdrawn_external_id"] for withdrawal in ordered
        ],
        "validation": validation,
        "policy": "verified_intervals_withdrawal",
    }
    if snapshot is not None:
        result["writer_contract_snapshot"] = snapshot["snapshot_dir"]
    return result


def set_baseline(
    state_dir: Path | str,
    *,
    context: dict[str, Any],
    baseline: dict[str, Any],
    event: dict[str, Any],
) -> dict[str, Any]:
    """Persist a new `athlete_baseline` into the current PlanState.

    This is a thin convenience wrapper around `apply_decision`, not a second
    persistence path: it only relieves the caller from re-copying every unrelated
    session verbatim just to change one athlete-level field. It reads the current
    PlanState, replaces `athlete_baseline`, lets the same version-diff rule every
    other change uses decide whether the version bumps, and then commits through
    `apply_decision` so idempotent replay and stale-version blocking behave exactly
    as they do for any other decision. No new stored object is introduced; the
    baseline lives inside PlanState like everything else in the store.
    """
    if event.get("mode") != "plan_cycle":
        raise StateStoreError("set-baseline requires a DecisionEvent with mode 'plan_cycle'")
    status = status_store(state_dir)
    before = status["current_plan"]
    after = copy.deepcopy(before)
    after["athlete_baseline"] = baseline
    changed = canonical_hash(before) != canonical_hash(after)
    after["version"] = before["version"] + (1 if changed else 0)
    return apply_decision(state_dir, context=context, after=after, event=event)
