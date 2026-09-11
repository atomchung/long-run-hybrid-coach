"""The operator-run half of export and deletion, for a request that arrives by email.

The in-conversation route is the better one wherever it works: the athlete's own
connection is the identity check, and it is a stronger check than an operator can perform
over email. It is not the route this release can promise. Issue #417 is the record: a
confirmed deletion in one client did not reach the service at all -- the model emitted the
call, the client's own approval layer refused it, and the athlete was left holding a
preview and no erasure. The product could not tell them that, because nothing about the
failure reached the product.

So the promise moves to a path this repository can actually execute end to end. An athlete
writes to the support address, the operator checks that the requester controls the account,
and the operator runs the *same* export and the *same* deletion the conversation would have
run. Nothing here is a second implementation of either: ``owner_data.export_archive`` and
``owner_data.delete_owner`` are the ones the gateway calls, fence and tombstone included.
What this module adds is the part a conversation gets for free and an email does not --
knowing who is asking.

## What counts as proof, and why it is this

Three things an athlete can put in an email are **not** proof, and each is worth naming
because each looks like proof:

- **The Intervals.icu athlete id.** It is in a URL. Anybody who has seen the athlete's
  profile has it.
- **The display name.** Same, and two accounts of one person routinely share one.
- **The address the mail came from.** This product stores no email address (see
  ``docs/account-lifecycle.md``), so there is nothing to compare it against. An address
  that matches the athlete's Intervals.icu account still only proves the sender knew it.

What is left is the authorization itself. Completing the Intervals.icu consent for an
athlete requires signing in as that athlete, and this deployment records the instant it
happened (``identity.owner_authorization_instants``). So the operator names a window in a
private reply, the athlete reconnects inside it, and a row appearing in that window is the
proof. An impersonator can produce an athlete id, a name and an address; they cannot
produce that row.

Two properties make it workable rather than ceremonial:

- **It needs no tool call to succeed.** Reconnecting is the OAuth hop, not an MCP
  operation, so a request is not blocked by the very failure that made this route
  necessary. An athlete whose client refuses the deletion call can still authorize.
- **It asks for nothing secret.** No password, no API key, no token. The athlete does what
  they already do to use the product.

The residual risk is stated rather than engineered away: a window wide enough to catch a
reconnect the athlete made for their own reasons would verify an impersonator by
coincidence. That is why the window has a hard maximum here instead of a sentence in a
runbook, why both bounds are required, and why the evidence returned lists every instant
found so an operator can compare it against what the requester said they did.

## What this module refuses to do

No request field names an account except the provider athlete id, and it is resolved
through the registry rather than turned into a path -- the owner id never comes from
anything an email said. A deletion is bound to the digest of the preview the requester was
shown, the same binding the hosted route makes, so an operator cannot confirm a scope
nobody read. And an account that cannot be reached at Intervals.icu any more is still an
impasse: this route restores the identity check, it does not replace it.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Any

from . import owner_data
from .gateway import PROVIDER
from .identity import (
    IdentityError,
    owner_authorization_instants,
    owner_count,
    owner_for_provider_athlete,
    owner_identity_row_counts,
)
from .proposals import binding
from .store import canonical_hash, read_maintenance_fence, resolve_state_dir


# How long a verification window may be. A day is generous for an athlete reading mail in
# another timezone and still far too short to make an unrelated reconnect likely, which is
# the only way this check fails open. It is enforced rather than advised because the
# failure is silent: a window of a month looks exactly like a window of an hour in the
# command's output, and an operator working through a mail thread has nothing to catch it
# against.
MAX_VERIFICATION_WINDOW_SECONDS = 24 * 60 * 60

# What an operator may say to somebody whose control of the account is not yet
# established -- which is to say, to anybody who has only sent an email. It is one string
# for both outcomes on purpose: replying "no such account" to an unverified requester
# discloses who does and does not use this product to whoever is willing to guess athlete
# ids, and that is a membership disclosure made to a stranger, unprompted.
UNVERIFIED_REPLY = (
    "Before any data can be exported or deleted, this request needs to be linked to the "
    "Intervals.icu account it names. Please re-authorize Long Run Hybrid Coach at "
    "Intervals.icu -- reconnect it in the AI client you use -- and reply to this message "
    "once you have. Re-authorizing is the same consent you gave when you first connected; "
    "it does not change your plan and does not delete anything. Do not send a password, an "
    "API key, or an authorization token: they are never needed and will not be read."
)


class PrivacyRequestError(RuntimeError):
    """An emailed export or deletion request could not be served as asked."""


def parse_instant(value: str, *, field: str) -> dt.datetime:
    """Read one ISO-8601 bound, refusing anything without a timezone.

    A naive timestamp is the one input that would silently move the window by the
    operator's own offset, which in Asia/Taipei is eight hours -- long enough to swallow a
    reconnect the athlete never made. Refused rather than assumed to be UTC.
    """
    text = str(value).strip()
    try:
        parsed = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        raise PrivacyRequestError(
            f"{field} must be an ISO-8601 instant with a timezone, "
            f"for example 2026-09-11T14:00:00Z; got {value!r}"
        ) from None
    if parsed.tzinfo is None:
        raise PrivacyRequestError(
            f"{field} must carry a timezone (add Z for UTC); got {value!r}"
        )
    return parsed.astimezone(dt.timezone.utc)


def _iso(moment: dt.datetime) -> str:
    return (
        moment.astimezone(dt.timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _owner_for_athlete(identity_db: Path | str, athlete_id: str) -> str | None:
    athlete_id = str(athlete_id).strip()
    if not athlete_id:
        raise PrivacyRequestError("an Intervals.icu athlete id is required")
    try:
        return owner_for_provider_athlete(identity_db, PROVIDER, athlete_id)
    except IdentityError as exc:
        raise PrivacyRequestError(str(exc)) from exc


def open_request(
    identity_db: Path | str, *, athlete_id: str, now: dt.datetime
) -> dict[str, Any]:
    """What to ask the requester for, and what this deployment already knows. Reads nothing.

    Deliberately touches no store: at this point the requester is an email address and a
    number, and opening somebody's plan to answer them would be handling their data on an
    unproven claim.

    The two halves of the result are kept apart because they are addressed to different
    people. ``reply`` is the text that may go back to the requester, and it is the same
    text whether or not the account exists. ``operator_only`` is what the operator needs
    to work the request, and repeating any of it in a reply is how an unverified
    requester learns whether a given athlete uses this product.
    """
    owner_id = _owner_for_athlete(identity_db, athlete_id)
    # Rounded *up* to the next whole second, not truncated to this one. The operator
    # copies this bound into the fulfilment commands, which read second precision, so a
    # bound that rounded down would pull an authorization already recorded when this
    # command ran into the window -- and the account's standing connection is exactly the
    # thing the window exists to exclude. Rounding up makes everything the registry
    # already held strictly earlier than the window this request opens.
    opened_at = now.astimezone(dt.timezone.utc).replace(microsecond=0) + dt.timedelta(
        seconds=1
    )
    expires_at = opened_at + dt.timedelta(seconds=MAX_VERIFICATION_WINDOW_SECONDS)
    return {
        "athlete_id": str(athlete_id).strip(),
        "authorize_after": _iso(opened_at),
        "authorize_before_at_latest": _iso(expires_at),
        "reply": UNVERIFIED_REPLY,
        "operator_only": {
            "account_exists": owner_id is not None,
            "owner_id": owner_id,
            "note": (
                "Whether an account exists is not part of the reply. Send `reply` "
                "unchanged either way, and close the request unfulfilled if no "
                "authorization arrives in the window."
            ),
        },
    }


def verify_account_control(
    identity_db: Path | str,
    *,
    athlete_id: str,
    since: dt.datetime,
    until: dt.datetime,
    now: dt.datetime,
) -> dict[str, Any]:
    """Establish that the requester controls the Intervals.icu account, or refuse.

    Every bound is checked before the registry is read, so a malformed window is refused
    without disclosing anything about the account it named.

    The window must be closed (``until`` at or before now): an open window cannot be
    evidence, because the authorization that would satisfy it has not necessarily happened
    yet, and an operator reading "verified" from a window still running would be reading a
    claim about the future.
    """
    since = since.astimezone(dt.timezone.utc)
    until = until.astimezone(dt.timezone.utc)
    now = now.astimezone(dt.timezone.utc)
    if until <= since:
        raise PrivacyRequestError(
            "the verification window ends at or before it starts: "
            f"{_iso(since)} to {_iso(until)}"
        )
    span = (until - since).total_seconds()
    if span > MAX_VERIFICATION_WINDOW_SECONDS:
        raise PrivacyRequestError(
            f"the verification window spans {int(span)} seconds; at most "
            f"{MAX_VERIFICATION_WINDOW_SECONDS} is accepted, because a wider one can be "
            "satisfied by a reconnect the athlete made for their own reasons"
        )
    if until > now:
        raise PrivacyRequestError(
            f"the verification window is still open until {_iso(until)}; "
            "close it before reading it as evidence"
        )
    owner_id = _owner_for_athlete(identity_db, athlete_id)
    if owner_id is None:
        # Same wording an existing account with no authorization gets, for the same reason
        # `UNVERIFIED_REPLY` is one string: an operator who pastes a refusal into a reply
        # should not be pasting a membership disclosure.
        raise PrivacyRequestError(
            "no authorization for this request was recorded in the verification window; "
            "the request is not verified and nothing may be exported or deleted"
        )
    try:
        instants = owner_authorization_instants(identity_db, owner_id)
    except IdentityError as exc:
        raise PrivacyRequestError(str(exc)) from exc
    in_window = sorted(
        moment
        for moment in (
            parse_instant(recorded, field="a recorded authorization")
            for recorded in instants
        )
        if since <= moment <= until
    )
    if not in_window:
        raise PrivacyRequestError(
            "no authorization for this request was recorded in the verification window; "
            "the request is not verified and nothing may be exported or deleted"
        )
    # The owner id is deliberately absent. This block is the part of a result an operator
    # can quote back into the mail thread -- it is the evidence the request was verified --
    # and the internal name of the athlete's storage is not something to put in an email.
    # Callers that need the id resolve it from the registry (`_verified_owner`).
    return {
        "verified": True,
        "athlete_id": str(athlete_id).strip(),
        "window": {"since": _iso(since), "until": _iso(until)},
        "authorizations_in_window": len(in_window),
        "authorization_instants": [_iso(moment) for moment in in_window],
        "note": (
            "Control of the Intervals.icu account is what this proves. Compare the "
            "instants against what the requester said they did before releasing anything; "
            "more than one is not a failure, but it is worth a second look."
        ),
    }


def _verified_owner(
    identity_db: Path | str,
    *,
    athlete_id: str,
    since: dt.datetime,
    until: dt.datetime,
    now: dt.datetime,
) -> tuple[str, dict[str, Any]]:
    verification = verify_account_control(
        identity_db, athlete_id=athlete_id, since=since, until=until, now=now
    )
    owner_id = _owner_for_athlete(identity_db, athlete_id)
    if owner_id is None:  # pragma: no cover - verification already proved it resolves
        raise PrivacyRequestError(
            "this account stopped resolving between verifying the request and serving it"
        )
    return owner_id, verification


def export_request(
    state_root: Path | str,
    identity_db: Path | str,
    *,
    athlete_id: str,
    since: dt.datetime,
    until: dt.datetime,
    now: dt.datetime,
    hmac_key: bytes,
) -> dict[str, Any]:
    """The archive the athlete would have received in conversation. Reads only.

    ``owner_data.export_archive`` unchanged, including its ``excluded`` list: an operator
    handing over a copy states the same omissions the product states, because "this is
    everything we hold" and "this is everything we will show you" are still different
    claims and the email route does not get to blur them.

    The verification runs first and the archive is only built after it passes, so a
    refused request never reads the store at all.
    """
    owner_id, verification = _verified_owner(
        identity_db, athlete_id=athlete_id, since=since, until=until, now=now
    )
    archive = owner_data.export_archive(
        resolve_state_dir(owner_id, state_root=state_root),
        identity_db=identity_db,
        owner_id=owner_id,
        owner_reference=binding(owner_id, key=hmac_key),
    )
    return {"verification": verification, "archive": archive}


def deletion_scope(
    state_root: Path | str,
    identity_db: Path | str,
    *,
    athlete_id: str,
    since: dt.datetime,
    until: dt.datetime,
    now: dt.datetime,
) -> dict[str, Any]:
    """Exactly what a confirmed deletion would remove, and the digest that binds it.

    ``scope_digest`` is what makes the emailed confirmation mean something. The requester
    is shown this preview; the deletion will only run against a preview that still hashes
    to the same value. If the account moved in between -- they reported a lift, a session
    reconciled -- the digest stops matching and the operator previews again rather than
    erasing something nobody read. It is the hosted route's ``preview_hash`` check, made
    to work across a mail thread instead of across a proposal.

    A deletion the store would refuse -- an unreconciled delivery, a cutover in progress --
    is refused here too, at preview, which is where an athlete can still do something
    about it.
    """
    owner_id, verification = _verified_owner(
        identity_db, athlete_id=athlete_id, since=since, until=until, now=now
    )
    preview = owner_data.deletion_preview(
        resolve_state_dir(owner_id, state_root=state_root),
        identity_db=identity_db,
        owner_id=owner_id,
    )
    return {
        "verification": verification,
        "scope_digest": canonical_hash(preview),
        **preview,
    }


def apply_deletion(
    state_root: Path | str,
    identity_db: Path | str,
    *,
    athlete_id: str,
    since: dt.datetime,
    until: dt.datetime,
    now: dt.datetime,
    hmac_key: bytes,
    scope_digest: str,
    confirmed: bool,
) -> dict[str, Any]:
    """Erase the account the requester confirmed, then check that it is gone.

    Three gates, in this order, and the order is the point: the request is verified before
    any store is opened, the scope is recomputed and matched before anything is removed,
    and the result is read back afterwards rather than inferred from the call returning.

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
    owner_id, verification = _verified_owner(
        identity_db, athlete_id=athlete_id, since=since, until=until, now=now
    )
    state_dir = resolve_state_dir(owner_id, state_root=state_root)
    preview = owner_data.deletion_preview(
        state_dir, identity_db=identity_db, owner_id=owner_id
    )
    current = canonical_hash(preview)
    if current != digest:
        raise PrivacyRequestError(
            "this account changed after the scope the requester confirmed was computed "
            f"(confirmed {digest}, now {current}). Send them the new scope and take a "
            "fresh confirmation; nothing has been deleted."
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
        "verification": verification,
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
    "MAX_VERIFICATION_WINDOW_SECONDS",
    "UNVERIFIED_REPLY",
    "PrivacyRequestError",
    "apply_deletion",
    "deletion_scope",
    "export_request",
    "open_request",
    "parse_instant",
    "verify_account_control",
]
