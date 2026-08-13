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
import tempfile
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

# The athlete's timezone is defined once, where the context window already resolves it.
from .context_core import DEFAULT_TIMEZONE

from .validation import (
    ACTIONABLE_MATCH_STATUSES,
    delivery_session_content,
    validate_bundle,
    validate_decision_event,
    validate_plan_state,
)


STORE_SCHEMA_VERSION = "1.0"
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
    """Hash validation's single delivery-relevant session projection."""
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
    if root.exists() and (not root.is_dir() or any(root.iterdir())):
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
    if set(manifest) != required_manifest:
        errors.append("store.json fields do not match the V1 manifest")
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
                        if event.get("reason_codes") == ["delivery_verified"]:
                            errors.extend(
                                f"{path.name}: {error}"
                                for error in _delivery_transition_errors(previous_plan, plan, event)
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

    report = {
        "status": "passed" if not errors else "blocked",
        "errors": errors,
        "warnings": warnings,
        "plan_id": manifest.get("plan_id"),
        "current_version": manifest.get("current_version"),
        "event_count": max(len(commit_paths) - 1, 0),
        "policy": "private_repo_external_current_state",
    }
    return report, current_plan, event_index


def doctor_store(state_dir: Path | str) -> dict[str, Any]:
    report, _, _ = _inspect_store(state_dir)
    return report


def _local_today(today: dt.date | str | None) -> str:
    """The athlete's own calendar date, as an ISO string.

    "Next" is a question about a calendar, so it needs the athlete's calendar. UTC would
    answer with yesterday through the first eight hours of every Taipei morning -- which
    is when the day's session is decided. An unresolvable zone is refused rather than
    quietly answered in the wrong one.
    """
    if isinstance(today, dt.date):
        return today.isoformat()
    if isinstance(today, str) and today:
        return dt.date.fromisoformat(today).isoformat()
    try:
        zone = ZoneInfo(DEFAULT_TIMEZONE)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise StateStoreError(f"unknown timezone: {DEFAULT_TIMEZONE!r}") from exc
    return dt.datetime.now(zone).date().isoformat()


def status_store(state_dir: Path | str, *, today: dt.date | str | None = None) -> dict[str, Any]:
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
    """
    report, plan, _ = _inspect_store(state_dir)
    if report["status"] != "passed" or plan is None:
        raise StateStoreError("state store failed doctor", details=report)
    as_of = _local_today(today)
    actionable = sorted(
        (
            session for session in plan.get("week", {}).get("sessions", [])
            if session.get("match_status") in ACTIONABLE_MATCH_STATUSES
        ),
        key=lambda session: (session.get("scheduled_date", ""), session.get("session_id", "")),
    )
    upcoming = [s for s in actionable if s.get("scheduled_date", "") >= as_of]
    return {
        **report,
        "as_of_date": as_of,
        "next_session": upcoming[0] if upcoming else None,
        "elapsed_without_outcome": [s for s in actionable if s.get("scheduled_date", "") < as_of],
        "current_plan": plan,
    }


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
        raise StateStoreError("state store failed doctor", details=report)

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
    with _exclusive_lock(root):
        doctor, before, event_index = _inspect_store(root, ignore_lock=True)
        if doctor["status"] != "passed" or before is None:
            raise StateStoreError("state store failed doctor", details=doctor)
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
    return {
        "status": "passed",
        "idempotent_replay": False,
        "plan_id": after["plan_id"],
        "current_version": after["version"],
        "event_count": sequence - 1,
        "validation": validation,
        "policy": "private_repo_external_current_state",
    }


def apply_delivery_observations(
    state_dir: Path | str,
    *,
    observations: list[dict[str, Any]],
) -> dict[str, Any]:
    """Atomically append one verified Intervals delivery set to current state."""
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
    with _exclusive_lock(root):
        doctor, before, event_index = _inspect_store(root, ignore_lock=True)
        if doctor["status"] != "passed" or before is None:
            raise StateStoreError("state store failed doctor", details=doctor)

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
    return {
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
