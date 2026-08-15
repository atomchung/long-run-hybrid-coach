"""Which private state store belongs to which provider athlete -- and nothing else.

A gateway serving more than one athlete needs exactly one fact the single-user CLI never
needed: given a live provider token, whose store may this request touch. That fact needs
four rows, so this module has four tables and no framework around them:

- ``owners``: the product's own opaque owner ids. An owner id is a UUID and never carries
  provider meaning, so a provider that changes its athlete-id format cannot rename a
  store directory.
- ``provider_identities``: ``(provider, provider_athlete_id) -> owner_id``. Stable across
  re-authorization, which is what makes "the same athlete comes back tomorrow" resolve to
  the same store instead of a fresh empty one.
- ``token_fingerprints``: ``HMAC(access_token) -> owner_id``. The plaintext token is never
  written, logged, or returned; the fingerprint is the only stored form, and it is keyed
  so a stolen database still cannot be brute-forced back into tokens.
- ``token_scopes``: the normalized scope names returned for that fingerprint at exchange.
  They are a historical observation, not proof that the provider will still accept a token.

Intervals.icu issues no refresh tokens: re-authorizing mints a new access token, and the
provider may keep earlier tokens valid alongside it (multiple access tokens per app since
late 2023). ``record_token_fingerprint`` still deletes the owner's previous fingerprints
for the provider first -- one live connection per owner is this slice's own policy, so the
gateway recognizes the newest authorization only, whatever the provider still accepts.

This is not a credential vault and not an account system. There are no passwords, no
sessions, no roles, and no second provider abstraction waiting to be filled in.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import json
import os
import sqlite3
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


IDENTITY_SCHEMA_VERSION = "1.1"
_CONNECT_TIMEOUT_SECONDS = 10

_SCHEMA_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS owners (
        owner_id TEXT PRIMARY KEY,
        created_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS provider_identities (
        provider TEXT NOT NULL,
        provider_athlete_id TEXT NOT NULL,
        owner_id TEXT NOT NULL REFERENCES owners(owner_id),
        created_at TEXT NOT NULL,
        PRIMARY KEY (provider, provider_athlete_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS token_fingerprints (
        fingerprint TEXT PRIMARY KEY,
        owner_id TEXT NOT NULL REFERENCES owners(owner_id),
        provider TEXT NOT NULL,
        created_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS token_scopes (
        fingerprint TEXT PRIMARY KEY REFERENCES token_fingerprints(fingerprint),
        scope_names_json TEXT NOT NULL,
        recorded_at TEXT NOT NULL
    )
    """,
)


class IdentityError(RuntimeError):
    """An identity-registry operation was blocked."""


def _utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def _text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise IdentityError(f"{field} must be a non-empty string")
    return value


def token_fingerprint(access_token: str, *, hmac_key: bytes) -> str:
    """Return the keyed, one-way handle this registry stores instead of a token.

    HMAC rather than a plain digest on purpose: access tokens are drawn from a space small
    enough that an unkeyed hash of a leaked database is a lookup table away from the
    tokens themselves. The key lives only in the server process environment, never here.
    """
    _text(access_token, "access token")
    if not isinstance(hmac_key, (bytes, bytearray)) or not bytes(hmac_key):
        raise IdentityError("token fingerprint key must be non-empty bytes")
    return hmac.new(bytes(hmac_key), access_token.encode("utf-8"), hashlib.sha256).hexdigest()


@contextmanager
def _connect(db_path: Path | str, *, create: bool) -> Iterator[sqlite3.Connection]:
    """Open the registry, creating the file 0600 and the schema only when asked.

    ``create=False`` is what a read path uses: ``sqlite3.connect`` happily creates an
    empty database for a missing file, which would turn "this token is unknown" into a
    brand-new registry silently appearing on disk.
    """
    path = Path(db_path)
    if not path.exists():
        if not create:
            raise FileNotFoundError(path)
        path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        # Create the file before sqlite does, so it is never briefly world-readable.
        os.close(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600))
    connection = sqlite3.connect(path, timeout=_CONNECT_TIMEOUT_SECONDS, isolation_level=None)
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        if create:
            for statement in _SCHEMA_STATEMENTS:
                connection.execute(statement)
        yield connection
    finally:
        connection.close()


@contextmanager
def _write_transaction(db_path: Path | str) -> Iterator[sqlite3.Connection]:
    """One serialized write. ``BEGIN IMMEDIATE`` takes the write lock before reading, so a
    concurrent request cannot mint a second owner for the same athlete."""
    with _connect(db_path, create=True) as connection:
        connection.execute("BEGIN IMMEDIATE")
        try:
            yield connection
        except BaseException:
            connection.execute("ROLLBACK")
            raise
        connection.execute("COMMIT")


def lookup_or_create_owner(db_path: Path | str, provider: str, provider_athlete_id: str) -> str:
    """Return the owner id for one provider athlete, creating it on first sight.

    Idempotent by construction: the same ``(provider, provider_athlete_id)`` always
    resolves to the same owner id, which is what keeps a re-authorizing athlete on their
    existing store instead of an empty new one.
    """
    provider = _text(provider, "provider")
    provider_athlete_id = _text(provider_athlete_id, "provider athlete id")
    try:
        with _write_transaction(db_path) as connection:
            row = connection.execute(
                "SELECT owner_id FROM provider_identities WHERE provider = ? AND provider_athlete_id = ?",
                (provider, provider_athlete_id),
            ).fetchone()
            if row is not None:
                return str(row[0])
            owner_id = str(uuid.uuid4())
            created_at = _utc_now()
            connection.execute(
                "INSERT INTO owners (owner_id, created_at) VALUES (?, ?)",
                (owner_id, created_at),
            )
            connection.execute(
                "INSERT INTO provider_identities (provider, provider_athlete_id, owner_id, created_at)"
                " VALUES (?, ?, ?, ?)",
                (provider, provider_athlete_id, owner_id, created_at),
            )
            return owner_id
    except sqlite3.Error as exc:
        raise IdentityError(f"identity registry write failed: {exc}") from exc


def record_token_fingerprint(
    db_path: Path | str,
    fingerprint: str,
    owner_id: str,
    provider: str,
    *,
    scope_names: tuple[str, ...] | None = None,
) -> None:
    """Make one fingerprint the owner's only live connection for that provider.

    Deleting the owner's previous fingerprints first enforces this slice's
    one-active-connection policy. The provider may keep older tokens valid, but a token
    this registry no longer resolves cannot authenticate here -- widening to concurrent
    entries later means keeping per-entry fingerprints, not weakening this write.
    """
    fingerprint = _text(fingerprint, "fingerprint")
    owner_id = _text(owner_id, "owner_id")
    provider = _text(provider, "provider")
    try:
        with _write_transaction(db_path) as connection:
            known = connection.execute(
                "SELECT 1 FROM owners WHERE owner_id = ?", (owner_id,)
            ).fetchone()
            if known is None:
                raise IdentityError("cannot record a fingerprint for an unknown owner")
            prior = connection.execute(
                "SELECT fingerprint FROM token_fingerprints WHERE owner_id = ? AND provider = ?",
                (owner_id, provider),
            ).fetchall()
            connection.executemany("DELETE FROM token_scopes WHERE fingerprint = ?", prior)
            connection.execute(
                "DELETE FROM token_fingerprints WHERE owner_id = ? AND provider = ?", (owner_id, provider)
            )
            connection.execute(
                "INSERT OR REPLACE INTO token_fingerprints (fingerprint, owner_id, provider, created_at)"
                " VALUES (?, ?, ?, ?)",
                (fingerprint, owner_id, provider, _utc_now()),
            )
            if scope_names is not None:
                connection.execute(
                    "INSERT INTO token_scopes (fingerprint, scope_names_json, recorded_at) VALUES (?, ?, ?)",
                    (fingerprint, json.dumps(list(scope_names), separators=(",", ":")), _utc_now()),
                )
    except sqlite3.Error as exc:
        raise IdentityError(f"identity registry write failed: {exc}") from exc


def owner_for_provider_athlete(
    db_path: Path | str, provider: str, provider_athlete_id: str
) -> str | None:
    """Resolve one provider athlete to its owner, or ``None`` when it has never connected.

    The read-only twin of ``lookup_or_create_owner``. An operator command that has to name
    an owner's state directory needs the id, but must not be able to mint an owner: an
    owner that exists only because somebody typed an athlete id is an owner no
    authorization ever created, and its directory would be a store no token can reach.
    """
    provider = _text(provider, "provider")
    provider_athlete_id = _text(provider_athlete_id, "provider athlete id")
    try:
        with _connect(db_path, create=False) as connection:
            row = connection.execute(
                "SELECT owner_id FROM provider_identities"
                " WHERE provider = ? AND provider_athlete_id = ?",
                (provider, provider_athlete_id),
            ).fetchone()
    except FileNotFoundError:
        return None
    except sqlite3.Error as exc:
        raise IdentityError(f"identity registry read failed: {exc}") from exc
    return None if row is None else str(row[0])


def owner_for_fingerprint(db_path: Path | str, fingerprint: str) -> str | None:
    """Resolve one fingerprint to its owner, or ``None`` when nothing matches.

    ``None`` covers "no registry yet", "token never seen", and "token superseded by a
    newer authorization" alike -- all three mean the same thing to the caller, and telling
    them apart in the response would say which tokens once existed.
    """
    fingerprint = _text(fingerprint, "fingerprint")
    try:
        with _connect(db_path, create=False) as connection:
            row = connection.execute(
                "SELECT owner_id FROM token_fingerprints WHERE fingerprint = ?",
                (fingerprint,),
            ).fetchone()
    except FileNotFoundError:
        return None
    except sqlite3.Error as exc:
        raise IdentityError(f"identity registry read failed: {exc}") from exc
    return None if row is None else str(row[0])


def scopes_for_fingerprint(db_path: Path | str, fingerprint: str) -> tuple[str, ...] | None:
    """Return scope names saved at exchange, or ``None`` if this token predates recording."""
    fingerprint = _text(fingerprint, "fingerprint")
    try:
        with _connect(db_path, create=False) as connection:
            row = connection.execute(
                "SELECT scope_names_json FROM token_scopes WHERE fingerprint = ?", (fingerprint,)
            ).fetchone()
    except FileNotFoundError:
        return None
    except sqlite3.Error as exc:
        raise IdentityError(f"identity registry read failed: {exc}") from exc
    if row is None:
        return None
    try:
        value = json.loads(str(row[0]))
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise IdentityError("stored token scopes are invalid") from exc
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise IdentityError("stored token scopes are invalid")
    return tuple(value)
