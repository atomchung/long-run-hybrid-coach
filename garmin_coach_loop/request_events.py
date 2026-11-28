"""A 90-day record of the tool calls one deployment answered (issue #556).

The access line already says, per request, which account called which tool and how it
ended. It lives in the hosting platform's log: about seven days, and per deployment, so
a week after an athlete joined there is no answer to "where did their first plan stop".
This keeps the same facts, plus two the line does not carry, on the deployment's own
volume for as long as ``retention_days`` says.

What a row holds is closed by construction: a timestamp, the keyed log handle the access
line prints, the tool name, the gateway's outcome vocabulary, a refused field path made
only of lowercase identifiers and indices, the client origin the token was issued to, the
protocol revision, the Intervals calls spent and the duration. Never an argument, a
result, a detail sentence, a plan or anything an athlete said.

Operational, not coaching state. It is written by the transport after the response has
gone out, and nothing a tool reads ever reads it, so a tool that writes nothing to the
registry or the owner store still writes nothing there. Which products keep it is
``retention_days``; a deployment that keeps none never opens the file.
A failure to record is logged and swallowed: losing a row must never cost a request.
"""

from __future__ import annotations

import datetime as dt
import logging
import re
import sqlite3
import threading
from pathlib import Path
from typing import Any

from . import product_identity

LOGGER = logging.getLogger(__name__)

FILE_NAME = "request-events.db"

# How many days each product keeps (issue #556). The full product keeps 90; every other
# product keeps none and never opens the file, so the Plugin entry -- held to a reviewer's
# read-only reading of its tools (issue #408) -- records nothing at all. The privacy page
# states this number.
RETENTION_DAYS = {"hybrid": 90}


def retention_days(product: str) -> int:
    """Days of tool-call records a deployment of ``product`` keeps; 0 is none."""
    return RETENTION_DAYS.get(product_identity.validate_product(product), 0)

# Pruning is a range delete on an indexed column, cheap but not free, so it runs at most
# this often rather than on every row.
PRUNE_INTERVAL = dt.timedelta(hours=1)

_SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS tool_calls (
        at TEXT NOT NULL,
        account TEXT NOT NULL,
        tool TEXT NOT NULL,
        outcome TEXT,
        field TEXT,
        client TEXT,
        protocol TEXT,
        intervals_calls INTEGER,
        duration_ms INTEGER
    )
    """,
    "CREATE INDEX IF NOT EXISTS tool_calls_at ON tool_calls (at)",
    "CREATE INDEX IF NOT EXISTS tool_calls_account ON tool_calls (account, at)",
)

# A field path as this product's own validators spell it: `change_request.week.intent`,
# `week.sessions[2].plan.steps[0].duration_minutes`. Lowercase identifiers and numeric
# indices only, so free text -- a value, a sentence, a session label in any script --
# cannot pass through, whatever the message around it says.
_FIELD_PATH = re.compile(r"[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*|\[\d+\])+")
_MAX_FIELD_CHARACTERS = 160


def refused_field(detail: Any, validation_errors: Any = None) -> str | None:
    """The first field path a refusal names, or ``None`` when it names none.

    ``detail`` is a request refusal's sentence and ``validation_errors`` a validation
    report's list; the first path found in either, in that order, is the answer. Only
    the path is kept -- never the sentence it was found in.
    """
    candidates: list[str] = []
    if isinstance(detail, str):
        candidates.append(detail)
    if isinstance(validation_errors, list):
        candidates.extend(error for error in validation_errors if isinstance(error, str))
    for text in candidates:
        match = _FIELD_PATH.search(text)
        if match:
            return match.group(0)[:_MAX_FIELD_CHARACTERS]
    return None


def _utc_seconds(moment: dt.datetime) -> str:
    return moment.astimezone(dt.timezone.utc).replace(microsecond=0).isoformat()


class RequestEvents:
    """One deployment's tool-call record, kept for ``retention_days``."""

    def __init__(self, path: Path | str, *, retention_days: int):
        if retention_days <= 0:
            raise ValueError("a record kept for no days is not kept; do not construct one")
        self.path = Path(path)
        self.retention = dt.timedelta(days=retention_days)
        self._lock = threading.Lock()
        self._pruned_at: dt.datetime | None = None

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=5)
        for statement in _SCHEMA:
            connection.execute(statement)
        return connection

    def record(
        self,
        *,
        at: dt.datetime,
        account: str,
        tool: str,
        outcome: str | None,
        field: str | None = None,
        client: str | None = None,
        protocol: str | None = None,
        intervals_calls: int | None = None,
        duration_ms: int | None = None,
    ) -> bool:
        """Append one row and prune what has aged out. True when the row was written."""
        try:
            with self._lock:
                connection = self._connect()
                try:
                    with connection:
                        connection.execute(
                            "INSERT INTO tool_calls (at, account, tool, outcome, field, "
                            "client, protocol, intervals_calls, duration_ms) "
                            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                            (
                                _utc_seconds(at), account, tool, outcome, field, client,
                                protocol, intervals_calls, duration_ms,
                            ),
                        )
                        if self._pruned_at is None or at - self._pruned_at >= PRUNE_INTERVAL:
                            connection.execute(
                                "DELETE FROM tool_calls WHERE at < ?",
                                (_utc_seconds(at - self.retention),),
                            )
                            self._pruned_at = at
                finally:
                    connection.close()
            return True
        except (sqlite3.Error, OSError) as exc:
            LOGGER.warning("request event not recorded: %s", type(exc).__name__)
            return False


def read_rows(path: Path | str, *, since: dt.datetime | None = None) -> list[dict[str, Any]]:
    """Every kept row, oldest first, opened read-only. An absent file is no rows."""
    path = Path(path)
    if not path.is_file():
        return []
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        query = "SELECT * FROM tool_calls"
        parameters: tuple[Any, ...] = ()
        if since is not None:
            query += " WHERE at >= ?"
            parameters = (_utc_seconds(since),)
        return [dict(row) for row in connection.execute(query + " ORDER BY at", parameters)]
    finally:
        connection.close()


__all__ = ["FILE_NAME", "RETENTION_DAYS", "RequestEvents", "read_rows", "refused_field", "retention_days"]
