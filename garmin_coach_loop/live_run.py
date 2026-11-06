"""Ephemeral readings from a watch during one owner's run.

The caller authenticates the owner separately from the watch payload. A running watch
normally sends a sample every five seconds. Only the latest sample is kept, with a
short replay fence for superseded sessions. Nothing here logs or writes to disk.
"""
from __future__ import annotations

import math
import re
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any


SAMPLE_INTERVAL_SECONDS = 5
STALE_AFTER_SECONDS = 15
RETENTION_SECONDS = 300
MAX_FUTURE_SECONDS = 5
METRICS = ("heart_rate_bpm", "cadence_rpm", "speed_mps")
FIELDS = {"session_id", "seq", "observed_at", "state", *METRICS}
SESSION_ID = re.compile(r"[A-Za-z0-9_-]{1,80}")


def _validate(sample: dict[str, Any]) -> None:
    if set(sample) != FIELDS:
        raise ValueError("Expected session_id, seq, observed_at, state and all three nullable metrics")
    session = sample["session_id"]
    if not isinstance(session, str) or SESSION_ID.fullmatch(session) is None:
        raise ValueError("session_id must be 1–80 ASCII letters, digits, underscores or hyphens")
    for key in ("seq", "observed_at"):
        if type(sample[key]) is not int or sample[key] < 0:
            raise ValueError(f"{key} must be a nonnegative integer")
    if sample["state"] not in ("running", "paused", "ended"):
        raise ValueError("state must be running, paused or ended")
    for key in ("heart_rate_bpm", "cadence_rpm"):
        value = sample[key]
        if value is not None and (type(value) is not int or value < 0):
            raise ValueError(f"{key} must be a nonnegative integer or null")
    speed = sample["speed_mps"]
    if speed is not None and (type(speed) not in (int, float) or speed < 0
                              or (isinstance(speed, float) and not math.isfinite(speed))):
        raise ValueError("speed_mps must be a finite nonnegative number or null")


@dataclass
class _Latest:
    sample: dict[str, Any]
    arrival: float
    age_at_arrival: float
    retired: dict[str, float]


class LiveRunStore:
    """Thread-safe per-process state. Expired owners are evicted on access."""

    def __init__(self, *, clock: Callable[[], float] = time.time,
                 monotonic: Callable[[], float] = time.monotonic, max_owners: int = 1024):
        if type(max_owners) is not int or max_owners < 1:
            raise ValueError("max_owners must be a positive integer")
        self._clock = clock
        self._monotonic = monotonic
        self._max_owners = max_owners
        self._owners: OrderedDict[str, _Latest] = OrderedDict()
        self._lock = threading.Lock()

    def _expire(self, now: float) -> None:
        # Accepted arrivals are ordered, so reads and retransmissions cannot extend TTL.
        while self._owners and now - next(iter(self._owners.values())).arrival >= RETENTION_SECONDS:
            self._owners.popitem(last=False)

    @staticmethod
    def _check_owner(owner_id: str) -> None:
        if not isinstance(owner_id, str) or not owner_id.strip():
            raise ValueError("An owner is required")

    def ingest(self, owner_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        self._check_owner(owner_id)
        if not isinstance(payload, dict):
            raise ValueError("Expected a JSON object")
        sample = dict(payload)
        _validate(sample)
        session, seq, observed = sample["session_id"], sample["seq"], sample["observed_at"]
        receipt = {"session_id": session, "seq": seq, "status": "ignored"}
        with self._lock:
            now, arrival = self._clock(), self._monotonic()
            self._expire(arrival)
            if observed > now + MAX_FUTURE_SECONDS:
                raise ValueError("observed_at is too far in the future")
            current = self._owners.get(owner_id)
            retired = {} if current is None else current.retired
            if current is not None:
                previous = current.sample
                if session == previous["session_id"]:
                    if seq == previous["seq"]:
                        return {**receipt, "status": "duplicate"} if sample == previous else receipt
                    if seq < previous["seq"] or observed < previous["observed_at"] or previous["state"] == "ended":
                        return receipt
                elif observed <= previous["observed_at"] or retired.get(session, -math.inf) > arrival:
                    return receipt
            if observed <= now - RETENTION_SECONDS:
                return receipt
            if current is None and len(self._owners) >= self._max_owners:
                # Evicting an active owner would let an old packet start it again.
                raise ValueError("Live run capacity reached")
            if current is not None and session != current.sample["session_id"]:
                retired = {key: until for key, until in retired.items() if until > arrival}
                retired[current.sample["session_id"]] = arrival + RETENTION_SECONDS
            self._owners[owner_id] = _Latest(sample, arrival, max(0, now - observed), retired)
            self._owners.move_to_end(owner_id)
            return {**receipt, "status": "accepted"}

    def snapshot(self, owner_id: str) -> dict[str, Any] | None:
        self._check_owner(owner_id)
        with self._lock:
            now, arrival = self._clock(), self._monotonic()
            self._expire(arrival)
            current = self._owners.get(owner_id)
            if current is None:
                return None
            # Count travel time and elapsed time even if the server wall clock moves back.
            elapsed = arrival - current.arrival
            current.age_at_arrival = max(current.age_at_arrival, now - current.sample["observed_at"] - elapsed)
            age = current.age_at_arrival + elapsed
            stale = age > STALE_AFTER_SECONDS
            result = {**current.sample, "age_seconds": age, "freshness": "stale" if stale else "fresh"}
            if stale or current.sample["state"] != "running":
                result.update(dict.fromkeys(METRICS))
            return result

    def discard(self, owner_id: str) -> None:
        self._check_owner(owner_id)
        with self._lock:
            self._expire(self._monotonic())
            self._owners.pop(owner_id, None)
