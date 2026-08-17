"""One athlete's own copy of what this product holds, and the erasure of it.

Two requests that a hosted service has to be able to answer for itself: *show me what you
have* and *delete it*. The operator commands for both already existed
(``delete_owner_store``, ``delete_owner_identity``, ``delete-owner``), which is a fine
answer while there is one athlete and they own the machine, and not an answer at all once
there is a second one. This module is the part that can be reached by the athlete, over
the same authenticated boundary every other route uses.

Three rules shape it, and each is the thing that goes wrong when it is missing:

- **The owner is resolved from the credential, never from the request.** There is no
  field on either operation that names an athlete. That is not a validation choice that
  could be tightened later; it is the absence of the parameter that a cross-owner
  deletion would have to travel in (issues #6, #7).
- **An export is a copy, not a dump.** It carries what the athlete gave this product and
  what it decided, and never the material that identifies or authorises them: no token,
  no keyed fingerprint, no provider payload, no other owner. ``EXCLUDED`` says so in the
  archive itself, because "this is everything" and "this is everything we will show you"
  are different claims and only one of them is true.
- **A deletion states what it cannot reach before it runs.** Workouts already written to
  the athlete's own Intervals calendar are theirs and stay theirs; so does the
  authorization, which is revoked at Intervals and not here. A deletion that quietly
  implied otherwise would be the more damaging kind of wrong (AGENTS.md 8).

The append-only store and erasure are in genuine tension: the commit chain is what makes
a plan replayable, and deletion is the one operation that has to remove history. They
coexist by not sharing a path. Nothing here appends, no decision or delivery route
reaches any of it, and the removal is of the whole directory rather than of anything
inside it -- there is no edit to a commit anywhere in this module, so no ordinary write
path can be made to perform one.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Any

from . import athlete_evidence
from .identity import (
    delete_owner_identity,
    owner_identity_row_counts,
    owner_scope_name_sets,
    revoked_after,
)
from .store import (
    StateStoreError,
    canonical_hash,
    delete_owner_store,
    history_store,
    pending_delivery_attempt,
    read_current_plan,
)


ARCHIVE_VERSION = "1"

# What the delivery fence calls this operation when it refuses one. The store's default
# is the operator command name, which is the wrong thing to put in front of an athlete
# who has no shell to run it in.
_HOSTED_DELETION = "deleting your data"

# Stated in every archive. Each line is something an athlete could reasonably expect to
# find and will not, with the reason -- an omission nobody names reads as an oversight.
EXCLUDED: tuple[str, ...] = (
    "OAuth access tokens and their keyed fingerprints: this product stores no provider "
    "credential, and the fingerprints are one-way digests that cannot be turned back "
    "into one.",
    "Raw Intervals.icu payloads, GPS tracks, and activity files: they are read to build "
    "a context and never written down. Export them from Intervals.icu itself.",
    "Any other athlete's data, and the internal owner id naming this account's storage.",
)

# Stated in every deletion preview and receipt, for the same reason.
NOT_REMOVED: tuple[str, ...] = (
    "Workouts this product already wrote to your Intervals.icu calendar. They belong to "
    "your Intervals.icu account; remove them there if you want them gone.",
    "Your Intervals.icu authorization. Revoke it in Intervals.icu Settings under the "
    "applications you have authorized -- deleting data here does not withdraw a grant "
    "you gave there.",
    "Operational logs, which carry request paths and refusal reasons and no plan, "
    "health, or identity content at all (docs/ops/security-events.md).",
)


def _utc_iso(moment: dt.datetime) -> str:
    """ISO-8601 UTC, seconds precision -- the one timestamp format this archive uses."""
    return (
        moment.astimezone(dt.timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _plan_snapshot(state_dir: Path) -> tuple[dict[str, Any] | None, list[str]]:
    """The current plan and its history, or an explicit reason there is none.

    A never-initialized account is a fact to report. A store that exists and cannot be
    read is not the same fact, and is raised rather than reported as absence: an export
    that turned an unreadable store into ``null`` would hand the athlete an empty archive
    of a plan that is still there (AGENTS.md 3).
    """
    if not (Path(state_dir) / "store.json").is_file():
        return None, ["no PlanState exists for this account"]
    current = read_current_plan(state_dir)
    return (
        {
            "plan_id": current["plan_id"],
            "plan_version": current["current_version"],
            "current_plan": current["current_plan"],
        },
        [],
    )


def export_archive(
    state_dir: Path | str,
    *,
    identity_db: Path | str,
    owner_id: str,
    owner_reference: str,
) -> dict[str, Any]:
    """Everything this product holds for one owner, as the owner may have it.

    ``owner_reference`` is a keyed handle for this account rather than the owner id
    itself: an operator servicing a request can find the account from it, and an athlete
    who pastes their archive into a public issue has not published the name of a
    directory. The id is deliberately not in here (see ``EXCLUDED``).
    """
    state_dir = Path(state_dir)
    plan_state, unknowns = _plan_snapshot(state_dir)
    revisions = (
        history_store(state_dir)["revisions"] if plan_state is not None else []
    )
    counts = owner_identity_row_counts(identity_db, owner_id)
    revoked = revoked_after(identity_db, owner_id)
    revoked_after_iso = (
        None
        if revoked is None
        else _utc_iso(dt.datetime.fromtimestamp(revoked, tz=dt.timezone.utc))
    )
    scope_sets = owner_scope_name_sets(identity_db, owner_id)
    attempt = pending_delivery_attempt(state_dir) if state_dir.is_dir() else None
    return {
        "archive_version": ARCHIVE_VERSION,
        "owner_reference": owner_reference,
        "identity": {
            # Counts, not values. What is held is the athlete's question; what the rows
            # say is the thing that must not travel. All five identity tables, spread
            # from the one function the deletion preview's `identity_rows` also calls
            # (issue #139) -- so the two surfaces report the same coverage under the
            # same key names and cannot quietly drift apart.
            "provider": "intervals",
            **counts,
            # The instant, not the epoch integer the registry stores it as: this is read
            # by an athlete, not replayed by `revoke_owner_connections`. Null is "never
            # revoked", the same answer `revoked_after` itself gives.
            "revoked_after": revoked_after_iso,
            # What a live connection is allowed to do, never which connection: the join
            # that produces this lives inside `owner_scope_name_sets` and never selects a
            # fingerprint, so nothing here can be turned back into the value EXCLUDED
            # keeps out. Scope names are non-secret metadata; the fingerprint stays out.
            "token_scope_names": [list(names) for names in scope_sets],
        },
        "plan_state": plan_state,
        "decision_history": revisions,
        "athlete_evidence": athlete_evidence.load_evidence(state_dir),
        "unresolved_delivery": None if attempt is None else attempt.get("attempt_id"),
        "excluded": list(EXCLUDED),
        "unknowns": unknowns,
    }


def deletion_preview(
    state_dir: Path | str, *, identity_db: Path | str, owner_id: str
) -> dict[str, Any]:
    """Exactly what a confirmed deletion would remove, computed the same way it removes it.

    ``delete_owner_store(confirm=False)`` is the store half's own preview, so the two
    cannot disagree about what the directory holds -- including its refusal while a
    delivery reservation is open, which surfaces here rather than at the moment of
    deletion. An athlete should not confirm an erasure that is going to be refused.
    """
    state_dir = Path(state_dir)
    store = delete_owner_store(state_dir, confirm=False, operation=_HOSTED_DELETION)
    plan_state, _ = _plan_snapshot(state_dir)
    revisions = history_store(state_dir)["revisions"] if plan_state is not None else []
    evidence = athlete_evidence.load_evidence(state_dir)
    return {
        "removes": {
            "plan_id": None if plan_state is None else plan_state["plan_id"],
            "plan_versions": len(revisions),
            "reported_strength_sessions": len(evidence["strength_reports"]),
            "reported_availability": evidence["availability"]["recurring"] is not None
            or bool(evidence["availability"]["week_overrides"]),
            "stored_profile": evidence["profile"] is not None,
            "identity_rows": owner_identity_row_counts(identity_db, owner_id),
            "stored_snapshots": store["snapshots_dir_existed"],
        },
        "not_removed": list(NOT_REMOVED),
        "reversible": False,
    }


def delete_owner(
    state_dir: Path | str,
    *,
    identity_db: Path | str,
    owner_id: str,
    owner_reference: str,
    now: dt.datetime,
) -> dict[str, Any]:
    """Erase one owner: their whole store first, then the rows that reach it.

    The order is the recoverable one. Removing the identity rows first and failing before
    the store would leave a directory of somebody's training history that no credential
    can reach and no request can delete -- data outliving every way of asking for it. The
    other way round, a failure leaves rows pointing at nothing, which the next attempt
    finishes; both halves are idempotent, so the retry is the repair.

    The receipt names counts and a keyed reference. It carries no owner id, no
    fingerprint, no plan content, and nothing about the athlete's training -- an audit
    record of a deletion should not be the last surviving copy of what was deleted.

    On "idempotent", precisely: repeating this has no further effect, which is what the
    tool's annotation claims. It is not a repeat the *same credential* can perform --
    that credential's rows are gone, so the next request is refused before it reaches
    here. What the repeat path is actually for is the half-finished case: a failure
    between the two removals leaves rows still resolving, and the retry finishes the job.
    """
    store = delete_owner_store(state_dir, confirm=True, operation=_HOSTED_DELETION)
    identity = delete_owner_identity(identity_db, owner_id)
    # The store's own lock does not cover the whole of a concurrent write: the evidence
    # writers create the owner directory *before* taking it, so one that was mid-flight
    # can put the directory back after `rmtree` and leave a file behind. Sweeping here,
    # after the identity rows are gone, is what makes that a closed window rather than a
    # narrow one -- the credential that wrote it no longer resolves and no new request
    # can arrive. Reported, never silent: state that came back after a deletion is
    # exactly the thing an athlete is entitled to be told about.
    resurrected = Path(state_dir).exists()
    if resurrected:
        delete_owner_store(state_dir, confirm=True, operation=_HOSTED_DELETION)
    return {
        "deleted": store["state_dir_removed"] or bool(identity["owners"]),
        "receipt_id": "gcd-"
        + canonical_hash(
            {
                "owner_reference": owner_reference,
                "at": now.astimezone(dt.timezone.utc).replace(microsecond=0).isoformat(),
            }
        )[:16],
        "removed": {
            "state_directory": store["state_dir_removed"],
            "state_written_during_deletion": resurrected,
            "stored_snapshots": store["snapshots_dir_removed"],
            "identity_rows": identity,
        },
        "not_removed": list(NOT_REMOVED),
    }


__all__ = [
    "ARCHIVE_VERSION",
    "EXCLUDED",
    "NOT_REMOVED",
    "StateStoreError",
    "delete_owner",
    "deletion_preview",
    "export_archive",
]
