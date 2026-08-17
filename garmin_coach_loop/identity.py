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
provider keeps earlier tokens valid alongside it (multiple access tokens per app since
late 2023). ``record_token_fingerprint`` records each one beside the others, so an owner's
live connections are exactly the ones the provider still accepts. An athlete who connects
this product twice -- one agent entry, one MCP client -- has two working connections, not
whichever they authorized last.

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
    """
    CREATE TABLE IF NOT EXISTS owner_revocations (
        owner_id TEXT PRIMARY KEY REFERENCES owners(owner_id),
        revoked_after INTEGER NOT NULL
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


def ensure_registry(db_path: Path | str) -> None:
    """Open the registry, creating the file and schema when either is missing, then close.

    A gateway process calls this once at startup, before it binds a socket, so a state
    root mounted for the first time -- or one whose ``identity.db`` was lost -- fails
    startup with a clear reason instead of first surfacing as a 500 on somebody's sign-in.
    Reuses exactly the ``create=True`` path ``_write_transaction`` already takes on every
    write (see above); this just opens it once with nothing to write.
    """
    try:
        with _connect(db_path, create=True):
            pass
    except OSError as exc:
        # `strerror`, not `str(exc)`: the latter interpolates the full path, and the
        # caller of this function never echoes a configured value back either.
        raise IdentityError(f"identity registry is unusable: {exc.strerror or exc}") from exc
    except sqlite3.Error as exc:
        raise IdentityError(f"identity registry is unusable: {exc}") from exc


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
    """Add one fingerprint to the owner's live connections for that provider.

    Per-entry rather than one-at-a-time: an owner's earlier fingerprints are left in
    place, because Intervals leaves the tokens behind them working and a registry that
    forgot them would log the athlete out of one entry every time they connected another.
    Two clients therefore hold two tokens, both resolving to the same store.

    Recording the same fingerprint twice is idempotent. Its scope row is replaced rather
    than inserted beside itself, and is cleared first so the fingerprint row can be
    rewritten without a foreign key pointing at the version being replaced.
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
            connection.execute(
                "DELETE FROM token_scopes WHERE fingerprint = ?", (fingerprint,)
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


def forget_token_fingerprint(db_path: Path | str, fingerprint: str) -> bool:
    """Drop one connection, leaving the owner and every other connection intact.

    This is what an observed revocation does. The product holds no provider credential to
    invalidate -- only the keyed fingerprint that says which store a token opens -- so
    "the credential no longer works" is expressed by no longer recognising it. The next
    call from that token is then a plain ``401`` with the challenge that restarts
    authorization, rather than a provider error the client can only report.

    Deliberately one fingerprint and not the owner's set: an athlete connected through two
    entries has two tokens, and one of them being revoked says nothing about the other
    (``record_token_fingerprint``). Deliberately not the owner either: a revoked
    connection is not a deletion request, and the plan stays exactly where it was
    (docs/account-lifecycle.md).

    Returns whether a row was actually removed, so a caller can tell "this revocation was
    news" from "already forgotten" without a second read. Idempotent, and a registry that
    does not exist has nothing to forget.
    """
    fingerprint = _text(fingerprint, "fingerprint")
    if not Path(db_path).exists():
        return False
    try:
        with _write_transaction(db_path) as connection:
            connection.execute(
                "DELETE FROM token_scopes WHERE fingerprint = ?", (fingerprint,)
            )
            removed = connection.execute(
                "DELETE FROM token_fingerprints WHERE fingerprint = ?", (fingerprint,)
            ).rowcount
    except sqlite3.Error as exc:
        raise IdentityError(f"identity registry write failed: {exc}") from exc
    return bool(removed)


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

    ``None`` covers "no registry yet" and "token never seen" alike -- both mean the same
    thing to the caller, and telling them apart in the response would say which tokens
    once existed.
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


def _no_owner_rows() -> dict[str, int]:
    """Every identity table at zero, for a registry or an owner that does not exist.

    Named rather than repeated, so a table added to ``_owner_row_counts`` cannot be
    missing from the two "nothing here" answers and quietly change their shape.
    """
    return {
        "owners": 0,
        "provider_identities": 0,
        "token_fingerprints": 0,
        "token_scopes": 0,
        "owner_revocations": 0,
    }


def _owner_row_counts(connection: sqlite3.Connection, owner_id: str) -> dict[str, int]:
    """This owner's row count in every identity table, in one connection.

    Shared by the deletion preview and the deletion itself so both agree on exactly what
    "this owner's rows" means -- the preview a `delete-owner --confirm` promises must be
    the same query the delete runs, not a second restatement of it.
    """
    owners = connection.execute(
        "SELECT COUNT(*) FROM owners WHERE owner_id = ?", (owner_id,)
    ).fetchone()[0]
    provider_identities = connection.execute(
        "SELECT COUNT(*) FROM provider_identities WHERE owner_id = ?", (owner_id,)
    ).fetchone()[0]
    token_fingerprints = connection.execute(
        "SELECT COUNT(*) FROM token_fingerprints WHERE owner_id = ?", (owner_id,)
    ).fetchone()[0]
    token_scopes = connection.execute(
        "SELECT COUNT(*) FROM token_scopes WHERE fingerprint IN "
        "(SELECT fingerprint FROM token_fingerprints WHERE owner_id = ?)",
        (owner_id,),
    ).fetchone()[0]
    owner_revocations = connection.execute(
        "SELECT COUNT(*) FROM owner_revocations WHERE owner_id = ?", (owner_id,)
    ).fetchone()[0]
    return {
        "owners": owners,
        "provider_identities": provider_identities,
        "token_fingerprints": token_fingerprints,
        "token_scopes": token_scopes,
        "owner_revocations": owner_revocations,
    }


def owner_identity_row_counts(db_path: Path | str, owner_id: str) -> dict[str, int]:
    """Count this owner's rows in every identity table, for a deletion preview or receipt.

    Zero counts, not an error, when the registry does not exist yet or has never seen
    this owner: "nothing here" is a normal answer for an operator checking before a
    delete, the same way ``owner_for_fingerprint`` answers an unknown token with ``None``
    rather than raising -- and, like every other read path here, this never creates the
    registry file merely by being asked about an owner that turns out not to be in it.
    """
    owner_id = _text(owner_id, "owner_id")
    try:
        with _connect(db_path, create=False) as connection:
            return _owner_row_counts(connection, owner_id)
    except FileNotFoundError:
        return _no_owner_rows()
    except sqlite3.Error as exc:
        raise IdentityError(f"identity registry read failed: {exc}") from exc


def delete_owner_identity(db_path: Path | str, owner_id: str) -> dict[str, int]:
    """Remove one owner's rows from every identity table, in a single transaction.

    Issue #6's operator deletion, identity half. Deletes child rows before parents, the
    same order ``record_token_fingerprint`` already relies on to satisfy the schema's own
    foreign keys: ``token_scopes`` for this owner's fingerprints, then
    ``token_fingerprints``, then the ``provider_identities`` mapping, then ``owners``
    itself. Returns the count actually removed from each table -- never a token or
    fingerprint value -- which is the whole of the operator's audit receipt (the deletion contract's
    "minimal audit receipt without sensitive data").

    Idempotent and side-effect-free on an owner this registry has never seen: zero rows
    deleted everywhere is a normal result, not an error, and a missing registry file is
    never created merely by asking it to delete an owner that was never in it.
    """
    owner_id = _text(owner_id, "owner_id")
    if not Path(db_path).exists():
        return _no_owner_rows()
    try:
        with _write_transaction(db_path) as connection:
            counts = _owner_row_counts(connection, owner_id)
            connection.execute(
                "DELETE FROM token_scopes WHERE fingerprint IN "
                "(SELECT fingerprint FROM token_fingerprints WHERE owner_id = ?)",
                (owner_id,),
            )
            connection.execute("DELETE FROM token_fingerprints WHERE owner_id = ?", (owner_id,))
            # An owner who ever used `revoke-connections` has a row here, and it holds a
            # foreign key to `owners`. Deleting them without it does not leave an orphan,
            # it fails -- so the one athlete who signed every client out would be the one
            # who could not then delete their account.
            connection.execute("DELETE FROM owner_revocations WHERE owner_id = ?", (owner_id,))
            connection.execute("DELETE FROM provider_identities WHERE owner_id = ?", (owner_id,))
            connection.execute("DELETE FROM owners WHERE owner_id = ?", (owner_id,))
            return counts
    except sqlite3.Error as exc:
        raise IdentityError(f"identity registry write failed: {exc}") from exc


def revoke_owner_connections(
    db_path: Path | str, owner_id: str, *, now: int | None = None
) -> dict[str, int]:
    """Drop every recorded connection for one owner, keeping the owner and their plan.

    ``delete_owner_identity`` above answers a deletion request and takes the athlete with
    it. This answers a different one -- "sign every client out" -- and is the whole of it:
    every entry point resolves a request by looking its fingerprint up here, including the
    MCP entry, whose access token is a sealed envelope carrying the provider credential
    rather than anything stored. Removing the fingerprint therefore ends tokens this
    gateway already issued as well as the provider one behind them, without a revocation
    list to keep.

    Deleting the fingerprints is not by itself enough, and this is the subtle half.
    A gateway access token carries the provider credential rather than a fingerprint, so
    it resolves through whatever the registry holds *at the time of the call*. If the same
    provider credential is ever recorded again -- the athlete reconnecting on a token
    Intervals still accepts -- a token issued before the revocation would resolve once
    more. So the instant of revocation is recorded too, and every token issued before it
    is refused from here on regardless of which fingerprints exist. That is what makes
    "signed out" mean signed out rather than "until the next sign-in".

    What it deliberately does not do is reach Intervals. The provider's own tokens stay
    valid at the provider until the athlete revokes them there; what stops here is this
    product's use of them. Signing back in resolves to the same owner and the same
    PlanState, because ``provider_identities`` is untouched.
    """
    owner_id = _text(owner_id, "owner_id")
    revoked_after = int(dt.datetime.now(dt.timezone.utc).timestamp()) if now is None else int(now)
    if not Path(db_path).exists():
        return {"token_fingerprints": 0, "token_scopes": 0, "revoked_after": revoked_after}
    try:
        with _write_transaction(db_path) as connection:
            counts = _owner_row_counts(connection, owner_id)
            connection.execute(
                "DELETE FROM token_scopes WHERE fingerprint IN "
                "(SELECT fingerprint FROM token_fingerprints WHERE owner_id = ?)",
                (owner_id,),
            )
            connection.execute("DELETE FROM token_fingerprints WHERE owner_id = ?", (owner_id,))
            known = connection.execute(
                "SELECT 1 FROM owners WHERE owner_id = ?", (owner_id,)
            ).fetchone()
            if known is not None:
                # Never moves backwards: two revocations in the same second must not let
                # the later one widen what the earlier one closed.
                connection.execute(
                    "INSERT INTO owner_revocations (owner_id, revoked_after) VALUES (?, ?)"
                    " ON CONFLICT(owner_id) DO UPDATE SET revoked_after = MAX(revoked_after, excluded.revoked_after)",
                    (owner_id, revoked_after),
                )
            return {
                "token_fingerprints": counts["token_fingerprints"],
                "token_scopes": counts["token_scopes"],
                "revoked_after": revoked_after,
            }
    except sqlite3.Error as exc:
        raise IdentityError(f"identity registry write failed: {exc}") from exc


def revoked_after(db_path: Path | str, owner_id: str) -> int | None:
    """The instant this owner's connections were last revoked, or ``None`` if never.

    ``None`` for a registry created before this table existed, exactly as
    ``scopes_for_fingerprint`` answers for its own: a read path must not migrate a
    registry, and an owner who has never revoked anything is the same answer as an owner
    in a registry that could not record one.
    """
    owner_id = _text(owner_id, "owner_id")
    try:
        with _connect(db_path, create=False) as connection:
            table = connection.execute(
                "SELECT type FROM sqlite_master WHERE name = 'owner_revocations'"
            ).fetchone()
            if table is None:
                return None
            if table[0] != "table":
                raise IdentityError("owner revocation registry object is invalid")
            row = connection.execute(
                "SELECT revoked_after FROM owner_revocations WHERE owner_id = ?", (owner_id,)
            ).fetchone()
    except FileNotFoundError:
        return None
    except sqlite3.Error as exc:
        raise IdentityError(f"identity registry read failed: {exc}") from exc
    return None if row is None else int(row[0])


def scopes_for_fingerprint(db_path: Path | str, fingerprint: str) -> tuple[str, ...] | None:
    """Return exchange scopes, or ``None`` when they were never recorded.

    Identity registries created before ``token_scopes`` existed remain readable: this
    read path must not migrate them merely to improve a diagnostic. Other malformed
    schema or SQLite errors still propagate as ``IdentityError``.
    """
    fingerprint = _text(fingerprint, "fingerprint")
    try:
        with _connect(db_path, create=False) as connection:
            scope_object = connection.execute(
                "SELECT type FROM sqlite_master WHERE name = 'token_scopes'"
            ).fetchone()
            if scope_object is None:
                return None
            if scope_object[0] != "table":
                raise IdentityError("token scopes registry object is invalid")
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
