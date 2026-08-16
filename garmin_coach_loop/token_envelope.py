"""Sealed envelopes: a token this gateway issues, carrying a credential it never stores.

The MCP entry cannot hand a client the athlete's Intervals access token and call it a
bearer of its own. Passing an upstream credential straight through is exactly what the
protocol forbids: a client that leaks it leaks the whole Intervals account, and a token
this server did not mint carries no audience it could check. The obvious alternative --
mint an opaque token, keep the Intervals one in a table beside it -- would turn this
process into a credential vault, which the rest of the product deliberately is not: the
identity registry keeps a keyed fingerprint and never the token itself.

So the token *is* the storage. The Intervals credential is encrypted into the value the
gateway hands out and can only be read back with the gateway's own key. Nothing is
written down, a restarted process forgets nothing anyone needed, and a stolen envelope
is inert without the key.

The construction, and why each part is the standard one:

- **Two keys, derived.** ``token_hmac_key`` already fingerprints tokens elsewhere, so it
  is never used directly here. ``HMAC(master, label)`` derives one encryption key and one
  MAC key -- the expand half of HKDF -- so no single value both encrypts and authenticates.
- **A stream cipher out of HMAC.** The keystream is ``HMAC(enc_key, nonce || counter)``
  block after block, XORed into the plaintext. HMAC is a pseudorandom function; running
  one in counter mode is the textbook way to build a keystream from one.
- **Encrypt-then-MAC.** The tag covers the nonce and the ciphertext, and is checked
  before a single byte is decrypted. That order is the composition that stays secure
  whatever the cipher underneath it does.
- **Domain separation in the tag.** ``kind`` is part of the MAC input, so an envelope
  sealed as an authorization code can never open as an access token, whoever presents it.
- **A fresh 16-byte nonce per envelope**, from ``secrets``. A keystream cipher is only as
  safe as the promise that one keystream is used once.

Not AES: the standard library ships no block cipher, and this repository takes no
dependencies (AGENTS.md). Everything above comes from ``hmac`` and ``hashlib``. Should a
dependency ever be acceptable, AES-GCM replaces this file without changing a caller --
``seal``/``open_envelope`` are the whole surface.

Every failure raises the same error with the same message. An opener that explained
which check failed -- bad key, bad tag, bad JSON, too old -- would answer questions about
a value the holder is not supposed to be able to construct.
"""

from __future__ import annotations

import base64
import datetime as dt
import hashlib
import hmac
import json
import secrets
from typing import Any


# The three envelopes this product issues, named once and unpacked so the names and the
# labels cannot drift apart. They are listed here rather than passed as free strings
# because the label is a security boundary: two kinds that accidentally shared a label
# would be interchangeable.
KINDS: tuple[str, ...] = ("authorize_state", "authorization_code", "access_token")
AUTHORIZE_STATE, AUTHORIZATION_CODE, ACCESS_TOKEN = KINDS

_ENCRYPT_LABEL = b"mcp-envelope-encrypt"
_MAC_LABEL = b"mcp-envelope-mac"
# The colon terminates the kind, so no kind can ever be a prefix of another's tag input.
_DOMAIN = b"garmin-coach-loop/mcp-envelope/v1/"

_NONCE_BYTES = 16
_TAG_BYTES = hashlib.sha256().digest_size
_COUNTER_BYTES = 4

_REFUSED = "this is not an envelope this gateway issued"


class EnvelopeError(RuntimeError):
    """An envelope could not be opened, for a reason this error deliberately omits."""


def _key_material(key: bytes | str) -> bytes:
    if isinstance(key, str):
        key = key.encode("utf-8")
    if not isinstance(key, (bytes, bytearray)) or not bytes(key):
        raise EnvelopeError("an envelope key must be non-empty")
    return bytes(key)


def _derived_keys(key: bytes | str) -> tuple[bytes, bytes]:
    """Split the configured master key into one encryption key and one MAC key."""
    master = _key_material(key)
    return (
        hmac.new(master, _ENCRYPT_LABEL, hashlib.sha256).digest(),
        hmac.new(master, _MAC_LABEL, hashlib.sha256).digest(),
    )


def _label(kind: str) -> bytes:
    if kind not in KINDS:
        # Reachable only from this product's own code, so it names the mistake.
        raise EnvelopeError(f"unknown envelope kind: {kind!r}")
    return _DOMAIN + kind.encode("ascii") + b":"


def _keystream(enc_key: bytes, nonce: bytes, length: int) -> bytes:
    blocks = bytearray()
    counter = 0
    while len(blocks) < length:
        blocks += hmac.new(
            enc_key, nonce + counter.to_bytes(_COUNTER_BYTES, "big"), hashlib.sha256
        ).digest()
        counter += 1
    return bytes(blocks[:length])


def _xor(data: bytes, keystream: bytes) -> bytes:
    return bytes(left ^ right for left, right in zip(data, keystream))


def _tag(mac_key: bytes, label: bytes, nonce: bytes, ciphertext: bytes) -> bytes:
    return hmac.new(mac_key, label + nonce + ciphertext, hashlib.sha256).digest()


def _encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def seal(payload: dict[str, Any], *, kind: str, key: bytes | str) -> str:
    """Encrypt and authenticate one payload into a single opaque, URL-safe string.

    ``iat`` is the caller's to state, in unix seconds, and is required: an envelope with
    no issue time is one ``open_envelope`` could never expire.
    """
    if not isinstance(payload, dict) or not payload:
        raise EnvelopeError("an envelope needs a payload")
    issued = payload.get("iat")
    if isinstance(issued, bool) or not isinstance(issued, int):
        raise EnvelopeError("an envelope payload states its own iat in unix seconds")
    label = _label(kind)
    enc_key, mac_key = _derived_keys(key)
    plaintext = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    nonce = secrets.token_bytes(_NONCE_BYTES)
    ciphertext = _xor(plaintext, _keystream(enc_key, nonce, len(plaintext)))
    return _encode(nonce + ciphertext + _tag(mac_key, label, nonce, ciphertext))


def open_envelope(
    token: Any,
    *,
    kind: str,
    key: bytes | str,
    now: dt.datetime,
    max_age_seconds: int | None,
) -> dict[str, Any]:
    """Verify, decrypt and age-check one envelope, or refuse without saying why.

    ``max_age_seconds`` of ``None`` means this kind does not expire on its own -- an
    access token lives exactly as long as the credential inside it, which only the
    provider can end.
    """
    label = _label(kind)
    enc_key, mac_key = _derived_keys(key)
    if not isinstance(token, str) or not token:
        raise EnvelopeError(_REFUSED)
    try:
        raw = _decode(token)
    except (ValueError, TypeError) as exc:
        raise EnvelopeError(_REFUSED) from exc
    if len(raw) <= _NONCE_BYTES + _TAG_BYTES:
        raise EnvelopeError(_REFUSED)
    nonce, ciphertext, tag = (
        raw[:_NONCE_BYTES],
        raw[_NONCE_BYTES:-_TAG_BYTES],
        raw[-_TAG_BYTES:],
    )
    if not hmac.compare_digest(_tag(mac_key, label, nonce, ciphertext), tag):
        raise EnvelopeError(_REFUSED)
    # Only now, with the ciphertext proven to be ours and untouched, is anything decrypted.
    try:
        payload = json.loads(
            _xor(ciphertext, _keystream(enc_key, nonce, len(ciphertext))).decode("utf-8")
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise EnvelopeError(_REFUSED) from exc
    if not isinstance(payload, dict):
        raise EnvelopeError(_REFUSED)
    issued = payload.get("iat")
    if isinstance(issued, bool) or not isinstance(issued, int):
        raise EnvelopeError(_REFUSED)
    if max_age_seconds is not None:
        if now.astimezone(dt.timezone.utc).timestamp() - issued >= max_age_seconds:
            raise EnvelopeError(_REFUSED)
    return payload
