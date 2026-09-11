"""The operator-run half of export and deletion, for a request that arrives by email.

An export is still something the athlete does for themselves in the conversation, and
their own connection is a stronger identity check than an operator can perform over
email. **A whole-account deletion is this module and nothing else.** Issue #417 is the
record: a confirmed deletion in one client did not reach the service at all -- the model
emitted the call, the client's own approval layer refused it, and the athlete was left
holding a preview and no erasure. The product could not tell them that, because nothing
about the failure reached the product.

So the erasure moved to the path this repository can actually execute end to end, and the
tools that offered the other one were removed rather than left standing beside it. An
athlete writes to the support address, the operator satisfies themselves that the
requester is the athlete, and the operator runs the export and the deletion here. Neither
is a second implementation: ``owner_data.export_archive`` and ``owner_data.delete_owner``
are the same functions the gateway calls and called, fence and tombstone included.

## The identity check is a person's, and this module says so

An earlier draft made the check mechanical: the athlete re-authorized at Intervals.icu
inside a window the operator named, and code refused everything else. It was sound and it
was rejected, for a reason worth recording -- it needed two rounds of reliable email in
both directions before anybody got anything, and a mail thread with one maintainer is not
a protocol. The owner's decision on 2026-09-11 is the simpler one:

- **Ask for a screenshot** of the athlete's Intervals.icu Settings page, showing their
  athlete id and Long Run Hybrid Coach among the applications they have authorized. One
  message, no round trip, and only somebody signed in to that account can produce it.
- **Accept the athlete id alone** when they cannot produce one. Impersonation is out of
  scope by decision, not by oversight.

So there is no gate here that could refuse a request on identity, and this module does not
pretend otherwise. What it does instead is make the basis explicit: every command takes an
identity-evidence value, and it is carried into the export report and into the deletion
receipt. An erasure is irreversible, and "what did we look at before running it" should be
answerable afterwards from the receipt rather than from memory.

## What this module still refuses to do

No request field names an account except the provider athlete id, and it is resolved
through the registry rather than turned into a path -- the owner id never comes from
anything an email said. A deletion is bound to the digest of the preview the requester was
shown *and* to the account it was computed for, so neither a scope nobody read nor a
mistyped athlete id reaches an erasure. And the scope is the conversation's, unchanged:
workouts already on the
athlete's Intervals.icu calendar, and the authorization they granted there, are theirs and
are not touched.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Any

from . import owner_data
from .gateway import PROVIDER
from .identity import (
    IdentityError,
    owner_count,
    owner_for_provider_athlete,
    owner_identity_row_counts,
)
from .proposals import binding
from .store import canonical_hash, read_maintenance_fence, resolve_state_dir


# What an operator may have looked at, and the only two answers this product records. A
# closed set rather than free text: the value ends up in a deletion receipt, where "what
# was checked" has to still mean the same thing to whoever reads it months later.
IDENTITY_EVIDENCE: dict[str, str] = {
    "settings-screenshot": (
        "a screenshot of the requester's Intervals.icu Settings page showing this athlete "
        "id and Long Run Hybrid Coach among their authorized applications"
    ),
    "athlete-id-only": (
        "the Intervals.icu athlete id and nothing further; the requester could not reach "
        "that page, and impersonation is out of scope by decision"
    ),
}

# What the operator asks for in the first reply. One message, nothing secret, and nothing
# the requester has to time or coordinate.
FIRST_REPLY = (
    "To connect this request to the Intervals.icu account it names, please reply with a "
    "screenshot of your Intervals.icu Settings page showing your athlete id and Long Run "
    "Hybrid Coach among the applications you have authorized. If you cannot reach that "
    "page, say so and quote your athlete id instead. Do not send a password, an API key, "
    "or an authorization token: they are never needed and will not be read."
)


class PrivacyRequestError(RuntimeError):
    """An emailed export or deletion request could not be served as asked."""


def _owner_for_athlete(identity_db: Path | str, athlete_id: str) -> str | None:
    athlete_id = str(athlete_id).strip()
    if not athlete_id:
        raise PrivacyRequestError("an Intervals.icu athlete id is required")
    try:
        return owner_for_provider_athlete(identity_db, PROVIDER, athlete_id)
    except IdentityError as exc:
        raise PrivacyRequestError(str(exc)) from exc


def _identity_record(athlete_id: str, evidence: str) -> dict[str, str]:
    """The basis this request was served on, in the words it will be read back in."""
    evidence = str(evidence).strip()
    if evidence not in IDENTITY_EVIDENCE:
        raise PrivacyRequestError(
            f"identity evidence must be one of {', '.join(sorted(IDENTITY_EVIDENCE))}; "
            f"got {evidence!r}"
        )
    return {
        "athlete_id": str(athlete_id).strip(),
        "evidence": evidence,
        "checked": IDENTITY_EVIDENCE[evidence],
    }


def _scope_digest(
    preview: dict[str, Any], *, owner_id: str, hmac_key: bytes
) -> str:
    """Bind a deletion scope to the account it was computed for.

    Hashing the preview alone is not enough, and the failure is not theoretical: the
    preview carries counts and literals and names no account, so two athletes who have
    connected and not yet been given a plan produce *byte-identical* previews and
    therefore one digest. An operator who previewed Alice, pasted Alice's digest, and
    mistyped the athlete id on the confirming command would irreversibly delete Bob --
    and the receipt's `other_accounts_unchanged` would still read true, because exactly
    one account did go.

    The owner travels as its keyed reference rather than as the owner id, so a digest
    quoted in a mail thread still discloses no storage identifier. Any mismatch between
    the account previewed and the account being deleted now fails the same check a
    changed scope fails.
    """
    return canonical_hash(
        {"owner": binding(owner_id, key=hmac_key), "preview": preview}
    )


def _resolved_owner(identity_db: Path | str, athlete_id: str) -> str:
    owner_id = _owner_for_athlete(identity_db, athlete_id)
    if owner_id is None:
        raise PrivacyRequestError(
            f"no account has ever connected as {PROVIDER} athlete "
            f"{str(athlete_id).strip()}; there is nothing here to export or delete"
        )
    return owner_id


def open_request(
    identity_db: Path | str, *, athlete_id: str, now: dt.datetime
) -> dict[str, Any]:
    """What to ask the requester for, and what this deployment already knows. Reads no store.

    Deliberately touches no store: at this point the requester is an email address and a
    number, and opening somebody's plan to answer them would be handling their data before
    anybody has looked at anything.

    ``reply`` is the text that goes back, and it is the same either way; ``operator_only``
    is the part that does not. Telling somebody who has shown nothing that a given athlete
    id is registered here is a disclosure about that athlete, not about the requester.
    """
    owner_id = _owner_for_athlete(identity_db, athlete_id)
    return {
        "athlete_id": str(athlete_id).strip(),
        "opened_at": now.astimezone(dt.timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z"),
        "reply": FIRST_REPLY,
        "accepted_evidence": dict(IDENTITY_EVIDENCE),
        "operator_only": {
            "account_exists": owner_id is not None,
            "owner_id": owner_id,
            "note": (
                "Whether an account exists is not part of the reply. Send `reply` "
                "unchanged either way."
            ),
        },
    }


def export_request(
    state_root: Path | str,
    identity_db: Path | str,
    *,
    athlete_id: str,
    identity_evidence: str,
    hmac_key: bytes,
) -> dict[str, Any]:
    """The archive the athlete would have received in conversation. Reads only.

    ``owner_data.export_archive`` unchanged, including its ``excluded`` list: an operator
    handing over a copy states the same omissions the product states, because "this is
    everything we hold" and "this is everything we will show you" are still different
    claims and the email route does not get to blur them.
    """
    identity = _identity_record(athlete_id, identity_evidence)
    owner_id = _resolved_owner(identity_db, athlete_id)
    archive = owner_data.export_archive(
        resolve_state_dir(owner_id, state_root=state_root),
        identity_db=identity_db,
        owner_id=owner_id,
        owner_reference=binding(owner_id, key=hmac_key),
    )
    return {"identity": identity, "archive": archive}


def deletion_scope(
    state_root: Path | str,
    identity_db: Path | str,
    *,
    athlete_id: str,
    identity_evidence: str,
    hmac_key: bytes,
) -> dict[str, Any]:
    """Exactly what a confirmed deletion would remove, and the digest that binds it.

    ``scope_digest`` is what makes the emailed confirmation mean something. The requester
    is shown this preview; the deletion will only run against this account and a preview
    that still hashes to the same value. If the account moved in between -- they reported
    a lift, a session reconciled -- the digest stops matching and the operator previews
    again rather than erasing something nobody read. See ``_scope_digest`` for why the
    account is part of it.

    It is weaker than the hosted route's ``preview_hash``, and the difference is worth
    stating rather than glossed: a proposal is signed and expires after
    ``PROPOSAL_TTL_SECONDS``, while this is an unsigned digest with no expiry that an
    operator retypes. What it does catch is the scope moving and the wrong account being
    named; what it cannot catch is an operator who reruns the preview and confirms it to
    themselves.

    A deletion the store would refuse -- an unreconciled delivery, a cutover in progress --
    is refused here too, at preview, which is where an athlete can still do something
    about it.
    """
    identity = _identity_record(athlete_id, identity_evidence)
    owner_id = _resolved_owner(identity_db, athlete_id)
    preview = owner_data.deletion_preview(
        resolve_state_dir(owner_id, state_root=state_root),
        identity_db=identity_db,
        owner_id=owner_id,
    )
    return {
        "identity": identity,
        "scope_digest": _scope_digest(preview, owner_id=owner_id, hmac_key=hmac_key),
        **preview,
    }


def apply_deletion(
    state_root: Path | str,
    identity_db: Path | str,
    *,
    athlete_id: str,
    identity_evidence: str,
    now: dt.datetime,
    hmac_key: bytes,
    scope_digest: str,
    confirmed: bool,
) -> dict[str, Any]:
    """Erase the account the requester confirmed, then check that it is gone.

    The scope is recomputed and matched before anything is removed, and the result is read
    back afterwards rather than inferred from the call returning.

    The read-back is what an operator can put in a reply. ``owner_data.delete_owner``
    already refuses to issue a receipt unless the directory is absent; what is added here
    is the registry side of the same question -- no identity rows left, a tombstone in
    place, and exactly one fewer account in the registry than before. That last count is
    the only check that would notice a deletion which also took somebody else's account,
    and an operator working from a mail thread has no other way to see it.
    """
    if not confirmed:
        raise PrivacyRequestError(
            "deletion needs the requester's confirmation of the exact scope they were "
            "shown; re-read the scope and pass it back confirmed"
        )
    digest = str(scope_digest).strip()
    if not digest:
        raise PrivacyRequestError(
            "deletion needs the scope digest from the preview the requester confirmed"
        )
    identity = _identity_record(athlete_id, identity_evidence)
    owner_id = _resolved_owner(identity_db, athlete_id)
    state_dir = resolve_state_dir(owner_id, state_root=state_root)
    preview = owner_data.deletion_preview(
        state_dir, identity_db=identity_db, owner_id=owner_id
    )
    current = _scope_digest(preview, owner_id=owner_id, hmac_key=hmac_key)
    if current != digest:
        raise PrivacyRequestError(
            "this scope digest is not this account's current one (confirmed "
            f"{digest}, now {current}). Either the account changed after the requester "
            "confirmed it, or the digest belongs to a different account than "
            f"{identity['athlete_id']}. Check the athlete id against the preview you "
            "sent, then send them the current scope and take a fresh confirmation; "
            "nothing has been deleted."
        )
    owners_before = owner_count(identity_db)
    erased = owner_data.delete_owner(
        state_dir,
        identity_db=identity_db,
        owner_id=owner_id,
        owner_reference=binding(owner_id, key=hmac_key),
        now=now,
    )
    fence = read_maintenance_fence(state_dir)
    remaining = owner_identity_row_counts(identity_db, owner_id)
    owners_after = owner_count(identity_db)
    return {
        "identity": identity,
        "scope_digest": digest,
        **erased,
        "verified_after_deletion": {
            "state_directory_absent": not Path(state_dir).exists(),
            "identity_rows_remaining": remaining,
            "deletion_tombstone": bool(fence and fence.get("tombstone")),
            "accounts_before": owners_before,
            "accounts_after": owners_after,
            "other_accounts_unchanged": owners_after == owners_before - 1,
        },
    }


__all__ = [
    "FIRST_REPLY",
    "IDENTITY_EVIDENCE",
    "PrivacyRequestError",
    "apply_deletion",
    "deletion_scope",
    "export_request",
    "open_request",
]
