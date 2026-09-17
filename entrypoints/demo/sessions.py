"""Short-lived, process-local demo conversations that cannot see each other.

A ``session_id`` names one visitor's exploration: the turns they have taken, the previews
they have asked for, and their own copy of the fixture PlanState. It is not an account and
it is not a login -- the id is an opaque value the page generates, it grants nothing, and
knowing somebody else's would only reach a conversation about the same synthetic athlete.
What it must never do is let one visitor's exploration show up in another's, and that is
what this module is for.

Isolation is by copy, not by convention. A session's plan is a deep copy taken when the
session is created, so the fixture underneath cannot be reached from a turn even by a bug,
and two sessions share no object at all.

Bounded in three directions, because the endpoint is public and anonymous: a session
expires (TTL), a session can only take so many turns, and the store holds only so many
sessions at once -- the oldest-touched goes first when it is full. Memory only. There is
no volume, no database, and nothing here survives a deploy, which is the correct lifetime
for a playground and the reason the service can be redeployed without a migration.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import re
import threading
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any

from . import fixture


# An opaque random id, bounded and in a charset that is safe to put in a log line. Not a
# format the client has to know about beyond "random and URL-safe": it is checked so that
# an unbounded or newline-carrying value cannot become a store key or a log record.
SESSION_ID_PATTERN = re.compile(r"\A[A-Za-z0-9_-]{8,128}\Z")


def valid_session_id(value: Any) -> bool:
    return isinstance(value, str) and bool(SESSION_ID_PATTERN.match(value))


def session_fingerprint(session_id: str) -> str:
    """A short, stable, non-reversing handle for logs.

    The raw id never reaches a log line. It is not a secret, but it is the only thing that
    joins two log records to one visitor, and a hash prefix is enough to read a trace
    without writing down the value that would let anyone resume the conversation.
    """
    return hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:12]


class SessionLimit(Exception):
    """A session that has taken every turn it is allowed."""


class StoreFull(Exception):
    """A full store in which every conversation is mid-turn, so none may be evicted."""


@dataclass
class DemoSession:
    """One visitor's conversation, and the only mutable state this service holds."""

    session_id: str
    created_at: dt.datetime
    last_seen_at: dt.datetime
    plan_state: dict[str, Any]
    turns: int = 0
    # The model-facing conversation so far: role/content items in the order they were
    # said, bounded by ``max_turns_per_session`` above it.
    history: list[dict[str, Any]] = field(default_factory=list)
    # Exploratory state -- the previews this conversation asked for. Session-local by
    # construction: nothing reads it but this session's own next turn.
    previews: list[dict[str, Any]] = field(default_factory=list)
    # Held for the whole of one turn. Two requests arriving on one session id -- a
    # double-tapped button, a retry over a slow answer -- would otherwise each read the
    # history, spend several seconds in the model, and write back a version that never saw
    # the other. The second request is refused rather than queued: a demo turn takes
    # seconds, and a caller waiting on a lock is a caller holding a worker thread.
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    @property
    def fingerprint(self) -> str:
        return session_fingerprint(self.session_id)


class SessionStore:
    """A bounded, expiring map of session id to conversation. Thread-safe."""

    def __init__(self, *, ttl_seconds: int, max_sessions: int, max_turns: int) -> None:
        self._ttl = dt.timedelta(seconds=ttl_seconds)
        self._max_sessions = max_sessions
        self._max_turns = max_turns
        self._lock = threading.Lock()
        self._sessions: "OrderedDict[str, DemoSession]" = OrderedDict()

    def __len__(self) -> int:
        with self._lock:
            return len(self._sessions)

    def _expire(self, now: dt.datetime) -> None:
        cutoff = now - self._ttl
        for session_id in [
            key for key, value in self._sessions.items() if value.last_seen_at <= cutoff
        ]:
            del self._sessions[session_id]

    def get_or_create(self, session_id: str, *, now: dt.datetime) -> DemoSession:
        """This session, expiring what has gone stale and evicting if the store is full.

        An expired id does not resume: it comes back as a new conversation with a new copy
        of the plan. That is the honest behaviour for a TTL -- reviving the turns would
        make the limit a suggestion -- and it is why the reply to a long-abandoned tab
        starts over rather than half-remembering.
        """
        with self._lock:
            self._expire(now)
            session = self._sessions.get(session_id)
            if session is not None:
                session.last_seen_at = now
                self._sessions.move_to_end(session_id)
                return session
            while len(self._sessions) >= self._max_sessions:
                # The oldest one that is not answering. Evicting a session mid-turn does
                # not stop that turn: the next request for the same id builds a new session
                # with a new lock, so two model calls run for one conversation and the one
                # in flight writes its history into an object nothing will read again.
                victim = next(
                    (key for key, value in self._sessions.items() if not value.lock.locked()),
                    None,
                )
                if victim is None:
                    raise StoreFull("every demo conversation in this process is mid-turn")
                del self._sessions[victim]
            session = DemoSession(
                session_id=session_id,
                created_at=now,
                last_seen_at=now,
                plan_state=fixture.plan_state(),
            )
            self._sessions[session_id] = session
            return session

    def begin_turn(self, session: DemoSession) -> int:
        """Claim this session's next turn, or refuse when it has spent them all."""
        with self._lock:
            if session.turns >= self._max_turns:
                raise SessionLimit(
                    f"this demo conversation is limited to {self._max_turns} turns"
                )
            session.turns += 1
            return session.turns

    def refund_turn(self, session: DemoSession) -> None:
        """Give back a turn that produced no answer.

        A conversation is capped at a handful of turns, so a provider timeout that still
        spends one is a visitor losing part of the demo to something that was never their
        doing. Only called where nothing was written: the history is committed at the end
        of a turn, so a turn that failed left none.
        """
        with self._lock:
            if session.turns > 0:
                session.turns -= 1

    def drop(self, session_id: str) -> None:
        with self._lock:
            self._sessions.pop(session_id, None)

    def purge_expired(self, *, now: dt.datetime) -> int:
        with self._lock:
            before = len(self._sessions)
            self._expire(now)
            return before - len(self._sessions)
