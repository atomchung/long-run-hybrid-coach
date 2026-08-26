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
    owner_active_day_count,
    revoked_after,
)
from .store import (
    StateStoreError,
    canonical_hash,
    delete_owner_store,
    history_store,
    mark_owner_deleted,
    owner_maintenance_fence,
    pending_delivery_attempt,
    read_current_plan,
    refuse_deletion_during_maintenance,
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
            # The sixth table, stated here but never among the counts above: it is what
            # the operator counts accounts and usage frequency with, and an athlete asking
            # what is held about them should be told the product keeps how many days they
            # used it and which tools they reached for -- never what any call contained.
            # Distinct days, because that is what the key says; the per-tool rows behind it
            # are an implementation detail and counting those would overstate the total.
            "usage_days": owner_active_day_count(identity_db, owner_id),
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

    A cutover somebody else is running is the second such refusal, and it is stated in the
    same shape and for the same reason (issue #137): a preview takes no lock, so without
    this the erasure would be previewed as available and would then fail against a store
    that was being moved. This deletion's own tombstone is not one of these -- see
    ``refuse_deletion_during_maintenance``.
    """
    state_dir = Path(state_dir)
    refuse_deletion_during_maintenance(state_dir, _HOSTED_DELETION)
    store = delete_owner_store(state_dir, confirm=False, operation=_HOSTED_DELETION)
    plan_state, _ = _plan_snapshot(state_dir)
    revisions = history_store(state_dir)["revisions"] if plan_state is not None else []
    evidence = athlete_evidence.load_evidence(state_dir)
    return {
        "removes": {
            "plan_id": None if plan_state is None else plan_state["plan_id"],
            "plan_versions": len(revisions),
            "reported_strength_sessions": len(evidence["strength_reports"]),
            "body_measurements": len(evidence["body_measurements"]),
            "reported_activities": len(evidence["reported_activities"]),
            # Their own words about how they felt, counted like every other statement they
            # made. A preview that left these out would understate the erasure by exactly
            # the thing an athlete is most likely to think of as personal.
            "subjective_states": len(evidence["subjective_states"]),
            # Counted separately from the sessions they produced. An athlete who uploaded
            # eight years of Garmin exports should see that the uploads themselves go,
            # not only the sessions -- otherwise a deletion preview reads as leaving the
            # record of what they handed over behind.
            "imported_uploads": len(evidence["imports"]),
            "reported_availability": evidence["availability"]["recurring"] is not None
            or bool(evidence["availability"]["week_overrides"]),
            "stored_profile": evidence["profile"] is not None,
            # Counted here rather than left to the store directory going away with
            # everything else: these two are the longest-lived thing the athlete ever
            # states -- what they are training for, and how -- and a preview that failed
            # to name them would understate what confirming actually costs them.
            "long_term_goals": len(evidence["long_term_goals"]),
            "training_preferences": len(evidence["training_preferences"]),
            "identity_rows": owner_identity_row_counts(identity_db, owner_id),
            # A literal, and every part of that is deliberate. This preview is what the
            # deletion proposal hashes, so anything here that can differ between the
            # preview and the confirmation refuses the erasure -- and a usage counter
            # moves on the very two calls that make up the deletion. Deriving even a
            # boolean from it is not enough: a counter write is swallowed when it fails
            # (`CoachGateway._count_usage`), so `count > 0` could read false at the
            # preview, true at the confirmation, and block an athlete's erasure over
            # telemetry they cannot see. What is stated instead is what deletion *does*,
            # which is true at any row count including none. The number itself is
            # disclosed by the export, which nothing hashes.
            "usage_counters_removed": True,
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

    All of it under one owner maintenance fence (issue #137), the same fence a cutover
    takes, for the failure the old one-time sweep could not close. Removing the identity
    rows stops *new* requests from resolving this owner; it says nothing about a request
    that resolved a moment earlier and is still running. Those writers create the owner
    directory before they take the store lock, so one arriving after the sweep would
    recreate the directory and write a file into it that no credential could ever reach
    again. Under the fence it cannot: the lock is where every writer meets the fence, so a
    late writer can leave an empty directory behind and never anything in it. This removes
    that shell as its last act while it still holds the fence, and the fence then stays as
    a deletion tombstone -- ``store.mark_owner_deleted`` is where that decision and its
    reasons live.

    The receipt names counts and a keyed reference. It carries no owner id, no
    fingerprint, no plan content, and nothing about the athlete's training -- an audit
    record of a deletion should not be the last surviving copy of what was deleted. It is
    issued only when the owner directory is verifiably absent at that point, because the
    receipt's whole claim is that there is nothing left.

    On "idempotent", precisely: repeating this has no further effect, which is what the
    tool's annotation claims. It is not a repeat the *same credential* can perform --
    that credential's rows are gone, so the next request is refused before it reaches
    here. What the repeat path is actually for is the half-finished case: a failure
    between the two removals leaves rows still resolving, and the retry finishes the job
    by re-entering the tombstone the first attempt left.
    """
    state_dir = Path(state_dir)
    refuse_deletion_during_maintenance(state_dir, _HOSTED_DELETION)
    # A symlinked owner entry is the one shape this fence cannot cover, and covering it
    # would be theatre: every writer resolves through the link, so the fence it meets is
    # the one beside the store the link points at -- a store this owner does not own and
    # this deletion deliberately does not touch. Only the link itself is this owner's, so
    # the link goes first and the fence then holds the freed path for everything after it,
    # including against a late writer that would otherwise create a real directory there.
    linked = state_dir.is_symlink()
    unlinked = (
        delete_owner_store(state_dir, confirm=True, operation=_HOSTED_DELETION)
        if linked
        else None
    )
    with owner_maintenance_fence(
        state_dir, operation=_HOSTED_DELETION, tombstone=True
    ) as fence:
        if linked:
            store = unlinked
        else:
            store = delete_owner_store(
                state_dir,
                confirm=True,
                operation=_HOSTED_DELETION,
                # Its own fence, met at its own store lock: without the token this
                # removal would refuse itself.
                fence_token=fence["fence_id"],
            )
        # The point of no return. From here the fence is this deletion's permanent record
        # rather than a window, including if what follows fails.
        mark_owner_deleted(state_dir, fence)
        identity = delete_owner_identity(identity_db, owner_id)
        # Reported, never silent: a directory that came back during a deletion is exactly
        # the thing an athlete is entitled to be told about, even when -- as the fence
        # guarantees -- there was nothing inside it.
        resurrected = state_dir.exists()
        if resurrected:
            delete_owner_store(
                state_dir,
                confirm=True,
                operation=_HOSTED_DELETION,
                fence_token=fence["fence_id"],
            )
        if state_dir.exists():
            raise StateStoreError(
                "deleting your data did not finish: a directory for this account is "
                "still present after it was removed. Nothing can be written to it -- the "
                "account is fenced as deleted -- and asking again completes the erasure."
            )
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
