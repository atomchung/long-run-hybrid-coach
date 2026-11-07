"""Ephemeral readings from a watch during one owner's run.

The caller authenticates the owner separately from the watch payload. A running watch
normally sends a sample every five seconds. The current session's samples are kept in
memory so a read can show how the run has been going, with a short replay fence for
superseded sessions. Nothing here logs or writes to disk.

The trend is arithmetic over those samples. What a number means for this runner is the
model's call, so nothing here compares a reading to a limit.
"""
from __future__ import annotations

import math
import re
import threading
import time
from collections import OrderedDict, deque
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any


SAMPLE_INTERVAL_SECONDS = 5
STALE_AFTER_SECONDS = 15
RETENTION_SECONDS = 300
MAX_FUTURE_SECONDS = 5
METRICS = ("heart_rate_bpm", "cadence_rpm", "speed_mps")
FIELDS = {"session_id", "seq", "observed_at", "state", *METRICS}
# A watch build older than these simply has no trend.
OPTIONAL_METRICS = ("timer_ms", "distance_m", "altitude_m", "ascent_m", "descent_m", "power_w", "steps_done")
HISTORY_SAMPLES = 3 * 3600 // SAMPLE_INTERVAL_SECONDS
STOPPED_BELOW_MPS = 0.3
MILESTONE_SECONDS = 600
SESSION_ID = re.compile(r"[A-Za-z0-9_-]{1,80}")


def _validate(sample: dict[str, Any]) -> None:
    if not FIELDS <= set(sample) or set(sample) - FIELDS - set(OPTIONAL_METRICS):
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
    for key in OPTIONAL_METRICS:
        value = sample.get(key)
        if value is None:
            continue
        if type(value) not in (int, float) or not math.isfinite(value) or (value < 0 and key != "altitude_m"):
            raise ValueError(f"{key} must be a finite number or null")


def _mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def _steps_per_minute(raw: float) -> float:
    """The SDK documents rpm; a running value at or below 100 counts one foot and doubles."""
    return raw * 2 if raw <= 100 else raw


def _pace(seconds_per_km: float | None) -> str | None:
    if seconds_per_km is None:
        return None
    return f"{int(seconds_per_km // 60)}:{int(seconds_per_km % 60):02d}"


def _window(samples: list[dict[str, Any]]) -> dict[str, Any] | None:
    """One stretch of a run. A reading the watch did not send stays null, never zero."""
    if not samples:
        return None
    first, last = samples[0], samples[-1]
    seconds = (last["timer_ms"] - first["timer_ms"]) / 1000
    moved = None
    if first.get("distance_m") is not None and last.get("distance_m") is not None:
        moved = last["distance_m"] - first["distance_m"]
    climb = None
    if first.get("altitude_m") is not None and last.get("altitude_m") is not None:
        climb = last["altitude_m"] - first["altitude_m"]
    moving = [s for s in samples if (s["speed_mps"] or 0) >= STOPPED_BELOW_MPS]
    rates = [s["heart_rate_bpm"] for s in samples if s["heart_rate_bpm"]]
    third = max(len(rates) // 3, 1)
    change = None
    if len(rates) >= 3:
        change = round(_mean(rates[-third:]) - _mean(rates[:third]))
    heart_rate = _mean(rates)
    cadence = _mean([_steps_per_minute(s["cadence_rpm"]) for s in moving if s["cadence_rpm"]])
    return {
        "heart_rate_bpm": None if heart_rate is None else round(heart_rate),
        "heart_rate_change_bpm": change,
        "pace_per_km": _pace(seconds / (moved / 1000) if moved and moved > 0 and seconds > 0 else None),
        "cadence_spm": None if cadence is None else round(cadence),
        "climb_m": None if climb is None else round(climb, 1),
        "grade_pct": round(climb / moved * 100, 1) if climb is not None and moved and moved >= 30 else None,
        "stopped_seconds": round((len(samples) - len(moving)) * seconds / max(len(samples) - 1, 1)),
    }


def summarize_trend(history: list[dict[str, Any]]) -> dict[str, Any] | None:
    """How the run has gone so far, compact because a client carries every read."""
    if not history:
        return None
    latest = history[-1]
    timer = latest["timer_ms"]

    def since(seconds: int) -> list[dict[str, Any]]:
        return [s for s in history if s["timer_ms"] >= timer - seconds * 1000]

    blocks = []
    for start in range(0, int(timer // 600_000) + 1):
        chunk = [s for s in history if start * 600_000 <= s["timer_ms"] < (start + 1) * 600_000]
        window = _window(chunk)
        if window is not None:
            blocks.append({
                "from_minute": start * 10,
                "heart_rate_bpm": window["heart_rate_bpm"],
                "pace_per_km": window["pace_per_km"],
                "cadence_spm": window["cadence_spm"],
                "climb_m": window["climb_m"],
            })
    return {
        "elapsed_minutes": round(timer / 60_000, 1),
        "distance_km": None if latest.get("distance_m") is None else round(latest["distance_m"] / 1000, 2),
        "ascent_m": latest.get("ascent_m"),
        "descent_m": latest.get("descent_m"),
        "last_1_minute": _window(since(60)),
        "last_3_minutes": _window(since(180)),
        "last_10_minutes": _window(since(600)),
        "every_10_minutes": blocks,
    }


@dataclass
class _Latest:
    sample: dict[str, Any]
    arrival: float
    age_at_arrival: float
    retired: dict[str, float]
    history: deque[dict[str, Any]]


class LiveRunStore:
    """Thread-safe per-process state. Expired owners are evicted on access."""

    def __init__(self, *, clock: Callable[[], float] = time.time,
                 monotonic: Callable[[], float] = time.monotonic, max_owners: int = 1024,
                 sleep: Callable[[float], None] = time.sleep):
        if type(max_owners) is not int or max_owners < 1:
            raise ValueError("max_owners must be a positive integer")
        self._clock = clock
        self._monotonic = monotonic
        self._max_owners = max_owners
        self._sleep = sleep
        self._reads: dict[str, tuple[str | None, float, int]] = {}
        self._milestones: dict[str, tuple[str, int]] = {}
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
            same_session = current is not None and session == current.sample["session_id"]
            history = current.history if same_session else deque(maxlen=HISTORY_SAMPLES)
            if sample["state"] == "running" and sample.get("timer_ms") is not None:
                history.append(sample)
            self._owners[owner_id] = _Latest(sample, arrival, max(0, now - observed), retired, history)
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
                result.update(dict.fromkeys((*METRICS, *OPTIONAL_METRICS)))
            trend = summarize_trend(list(current.history))
            if trend is not None:
                result["trend"] = trend
            return result

    def milestone_due(self, owner_id: str) -> int | None:
        """The whole minutes of a mark this run has passed since one was last reported.

        A companion that says nothing and one that has gone away sound the same to a
        runner, so a regular mark gives the runner something to notice missing. A client
        joining mid-run starts counting from there rather than announcing the past.
        """
        self._check_owner(owner_id)
        with self._lock:
            current = self._owners.get(owner_id)
            if current is None or current.sample["state"] != "running" or not current.history:
                return None
            session = current.sample["session_id"]
            mark = int(current.history[-1]["timer_ms"] // (MILESTONE_SECONDS * 1000))
            last_session, last_mark = self._milestones.get(owner_id, (None, 0))
            self._milestones[owner_id] = (session, mark)
            if session != last_session or mark <= last_mark:
                return None
            return mark * MILESTONE_SECONDS // 60

    def hold_repeat_read(self, owner_id: str, seconds: float) -> int:
        """Answer a read repeated during a run only `seconds` after the previous answer.

        This is what lets a client that only acts when a tool returns stay with a runner
        who is not talking to it: it calls again straight away, and the answer arrives
        with the next stretch of the run. A first read and a read of a finished or absent
        session return at once. Returns how many reads this session has had.
        """
        self._check_owner(owner_id)
        while True:
            with self._lock:
                now = self._monotonic()
                self._expire(now)
                current = self._owners.get(owner_id)
                session = None if current is None else current.sample["session_id"]
                active = current is not None and current.sample["state"] != "ended"
                last_session, last_read, count = self._reads.get(owner_id, (None, -math.inf, 0))
                remaining = last_read + seconds - now if active and session == last_session else 0
                if remaining <= 0:
                    count = count + 1 if session == last_session else 1
                    self._reads[owner_id] = (session, now, count)
                    return count
            self._sleep(min(remaining, 1.0))

    def discard(self, owner_id: str) -> None:
        self._check_owner(owner_id)
        with self._lock:
            self._expire(self._monotonic())
            self._owners.pop(owner_id, None)
