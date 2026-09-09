"""Which Intervals account this bearer reaches, said in words a person recognizes.

The gateway already knows *securely* whose store a request may touch: the bearer's
fingerprint resolves to an owner, and `(provider, provider_athlete_id) -> owner` is the
identity key. None of that is legible. An athlete holding a normal account and a separate
review account cannot tell the two connections apart, and neither can the model in front
of them, so a workout can be prepared against one and pushed to the other -- which is
exactly what happened during multi-account testing (issue #396).

This module adds the missing half and deliberately nothing else:

- **A label, read live, never stored.** `describe` shapes what `GET /api/v1/athlete/0`
  returned into `Intervals.icu -- email -- name`. The email leads because it is the half
  that actually separates the two accounts: a person's own account and their review
  account are routinely called the same thing, and the address never is. The name follows
  as the cue they recognise faster. Both are display strings with one job -- letting a
  person recognise the account before something is written to it. Neither is ever an
  authorization input, an owner key, persisted, or logged; the caller reads them on
  demand for one response and drops them.
- **A binding, keyed, safe to carry.** `account_ref` is the deployment-keyed handle of
  the *identity row*, so a prepared delivery can name the account it targets and the
  apply can refuse a bearer that resolves to a different one. It is what travels in a
  delivery set instead of the raw athlete id, for the same reason the token registry
  stores a fingerprint instead of a token: a handle proves sameness without republishing
  the value, and the orchestration prompt tells the model never to display an athlete id.

The two halves are independent on purpose. The binding comes from the registry and is
always available; the label comes from the provider and may not be. A label that cannot
be resolved is reported as unresolved -- never as somebody else's name.
"""

from __future__ import annotations

import hashlib
import hmac
from typing import Any


PROVIDER_LABEL = "Intervals.icu"

# How a label read went, in one word the model can branch on. There is no fourth value,
# and `resolved` is the only one that carries an email or a name.
RESOLVED = "resolved"
UNAVAILABLE = "unavailable"
MISMATCH = "mismatch"

# Why a label is missing. Server-owned constants, never a provider body or an exception
# string: this rides in a tool result the model reads out loud.
NO_IDENTITY_ROW = "no_identity_row"
PROFILE_UNREADABLE = "profile_unreadable"
PROFILE_WITHOUT_LABEL = "profile_without_label"
PROFILE_WITHOUT_ATHLETE_ID = "profile_without_athlete_id"
DIFFERENT_ATHLETE = "profile_names_a_different_athlete"

_SEPARATOR = " — "


def account_ref(provider: str, provider_athlete_id: str, *, hmac_key: bytes) -> str:
    """The keyed handle a proposal carries instead of the provider's athlete id.

    Same construction and same reason as ``identity.token_fingerprint``: one deployment's
    key, a one-way function, and a value that is only ever compared with another value
    computed here. Two refs are equal exactly when they name one identity row, which is
    the whole question a delivery has to answer before it writes.
    """
    if not isinstance(provider, str) or not provider.strip():
        raise ValueError("provider must be a non-empty string")
    if not isinstance(provider_athlete_id, str) or not provider_athlete_id.strip():
        raise ValueError("provider athlete id must be a non-empty string")
    if not isinstance(hmac_key, (bytes, bytearray)) or not bytes(hmac_key):
        raise ValueError("account ref key must be non-empty bytes")
    material = f"{provider.strip()}:{provider_athlete_id.strip()}".encode("utf-8")
    return hmac.new(bytes(hmac_key), material, hashlib.sha256).hexdigest()


def binding(provider: str, provider_athlete_id: str, *, hmac_key: bytes) -> dict[str, str]:
    """The account half of a delivery set: which provider, and which account there."""
    return {
        "provider": provider,
        "account_ref": account_ref(provider, provider_athlete_id, hmac_key=hmac_key),
    }


def is_binding(value: Any) -> bool:
    """Whether a value has the exact shape ``binding`` produces, and nothing more."""
    return (
        isinstance(value, dict)
        and set(value) == {"provider", "account_ref"}
        and all(isinstance(item, str) and item.strip() for item in value.values())
    )


def _clean(value: Any) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _athlete_id(value: Any) -> str | None:
    """The provider's athlete id in the exact shape the registry stored it, or ``None``.

    Deliberately the same coercion the authorization used: ``_redeem_intervals_code``
    refuses an id that is absent or blank after ``str(...).strip()`` and registers what
    is left, so an id the provider sends as a number is registered as its digits. Reading
    it back under a stricter rule would make two halves of one product disagree about one
    field -- and the half that disagreed here would fail closed on every write, which is
    the expensive direction to be wrong in.

    ``None`` means the id cannot be compared at all, which is not the same answer as
    comparing it and finding somebody else.
    """
    if value is None:
        return None
    return str(value).strip() or None


def _display_name(profile: dict[str, Any]) -> str | None:
    """The account's own name, however this provider spelled it in the response.

    ``name`` is what intervals.icu returns today. The first/last fallback exists because a
    label that silently becomes "Intervals.icu -- email" is a label a person recognises
    less, and reconstructing it costs one line. It is the second half of the label, not
    the first: a name alone cannot tell one person's two accounts apart.
    """
    named = _clean(profile.get("name"))
    if named:
        return named
    parts = [_clean(profile.get("firstname")), _clean(profile.get("lastname"))]
    joined = " ".join(part for part in parts if part)
    return joined or None


def describe(
    *,
    registered_athlete_id: str | None,
    profile: dict[str, Any] | None,
    unreadable_reason: str | None = None,
) -> dict[str, Any]:
    """One connected account as a model reads it: a label, or an explicit reason there is none.

    ``registered_athlete_id`` is the identity row's own value -- the security key, not
    anything the provider said in this response. ``profile`` is what the live read
    returned, or ``None`` when it could not be read at all.

    The comparison in the middle is the point. A profile whose ``id`` is not the athlete
    this bearer is registered as does not get to supply the name shown before a write:
    that is the one failure this whole surface exists to prevent, so it is reported as
    ``mismatch`` and carries no email and no name. Every other failure is ``unavailable``
    with a reason. Neither is an error -- an athlete whose Settings permission is denied
    still gets their plan; they are told the account could not be named.

    That the comparison is the point is also why it is the narrow half. ``mismatch`` means
    the provider answered *for somebody else*, and only that: an id read under the same
    coercion the authorization used (``_athlete_id``) and found to be a different athlete.
    A response with no id to compare is ``unavailable``, because the difference between
    "this is the wrong account" and "this response did not say which account" is the
    difference between refusing a write and losing a label.

    A profile that carries a name and no address is still ``resolved``: the athlete is
    told which account this is as far as the provider allowed, which is what the state
    means. It is a weaker answer than a resolved label usually is, and the field
    descriptions say so -- ``email`` is ``null``, and two accounts sharing a display name
    are indistinguishable from that label alone. Inventing a fourth resolution for it
    would make every reader branch on a distinction the object already shows.
    """
    described: dict[str, Any] = {"provider": PROVIDER_LABEL}
    if registered_athlete_id is None:
        return {**described, "resolution": UNAVAILABLE, "reason": NO_IDENTITY_ROW}
    if profile is None:
        return {
            **described,
            "resolution": UNAVAILABLE,
            "reason": unreadable_reason or PROFILE_UNREADABLE,
        }
    answered = _athlete_id(profile.get("id"))
    if answered is None:
        # An id that is not there is not an id that is somebody else's. Saying `mismatch`
        # here would refuse every write on a response this code simply could not read the
        # identity out of, which is the one failure `unavailable` exists to keep cheap.
        return {**described, "resolution": UNAVAILABLE, "reason": PROFILE_WITHOUT_ATHLETE_ID}
    if answered != registered_athlete_id.strip():
        return {**described, "resolution": MISMATCH, "reason": DIFFERENT_ATHLETE}
    name = _display_name(profile)
    email = _clean(profile.get("email"))
    if name is None and email is None:
        return {**described, "resolution": UNAVAILABLE, "reason": PROFILE_WITHOUT_LABEL}
    return {
        **described,
        "resolution": RESOLVED,
        # Email first, here and in the label. One person's own account and their review
        # account can carry one display name; the address is what tells them apart, so it
        # is what a reader should reach first (issue #396).
        "email": email,
        "name": name,
        "label": _SEPARATOR.join(
            part for part in (PROVIDER_LABEL, email, name) if part is not None
        ),
    }
