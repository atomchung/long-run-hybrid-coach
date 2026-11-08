"""What one owner's watch sent during their latest run, held in memory for a recap.

The caller authenticates the owner separately from the watch payload. A running watch
normally sends a sample every five seconds. Every running sample of the latest session is
kept until 24 hours after its last sample, or until a newer session replaces it, so a read
just after the run can summarize all of it before any provider has synced the activity. A
replay fence keeps a superseded session from starting again. Nothing here logs or writes
to disk, and nothing here is read by a count, a plan match or a delivery.

The summary is arithmetic over those samples. What a number means for this runner is the
model's call; the only limits here are the ones that define a stop and a walk.
"""
from __future__ import annotations

import datetime as dt
import math
import re
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any


SAMPLE_INTERVAL_SECONDS = 5
RETENTION_SECONDS = 24 * 3600
MAX_FUTURE_SECONDS = 5
METRICS = ("heart_rate_bpm", "cadence_rpm", "speed_mps")
FIELDS = {"session_id", "seq", "observed_at", "state", *METRICS}
# A watch build older than these simply has no summary.
OPTIONAL_METRICS = ("timer_ms", "distance_m", "altitude_m", "ascent_m", "descent_m", "power_w", "steps_done")
# A day of samples at the watch's pace. A session past it is thinned, never cut, so the
# summary still spans the whole run.
MAX_RUN_SAMPLES = RETENTION_SECONDS // SAMPLE_INTERVAL_SECONDS
STOPPED_BELOW_MPS = 0.3
# Walking is a change of gait, not a pace: the watch's own cadence below this share of the
# run's median moving cadence. A slow runner's easy pace or a climb run at running cadence
# is not walking, and the share holds whether the watch counts one foot or both.
WALKING_CADENCE_SHARE = 0.8
MIN_STRETCH_SECONDS = 10
# A gap this long between samples says nothing about what happened inside it.
MAX_CLASSIFIED_GAP_SECONDS = 3 * SAMPLE_INTERVAL_SECONDS
# As in the provider's run_drift: thirds of a shorter run differ by warm-up.
MIN_THIRDS_SECONDS = 15 * 60
SESSION_ID = re.compile(r"[A-Za-z0-9_-]{1,80}")
NOTE = (
    f"Recorded during the run; held {RETENTION_SECONDS // 3600} h after last_sample_at. "
    "Totals and plan matching use Intervals only, which may list this same run once synced."
)


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


def _rounded(value: float | None) -> int | None:
    return None if value is None else round(value)


def _steps_per_minute(raw: float) -> float:
    """The SDK documents rpm; a running value at or below 100 counts one foot and doubles."""
    return raw * 2 if raw <= 100 else raw


def _pace(seconds: float, metres: float | None) -> int | None:
    return round(seconds / (metres / 1000)) if metres and metres > 0 and seconds > 0 else None


def _iso(epoch: float) -> str:
    moment = dt.datetime.fromtimestamp(epoch, dt.timezone.utc).replace(microsecond=0)
    return moment.isoformat().replace("+00:00", "Z")


def _moving(sample: dict[str, Any]) -> bool:
    """No speed reading is no evidence of stopping."""
    return sample["speed_mps"] is None or sample["speed_mps"] >= STOPPED_BELOW_MPS


def _block_index(sample: dict[str, Any]) -> int:
    """The ten minutes a sample closes: one taken at 10:00 ends the first block."""
    return int(max(sample["timer_ms"] - 1, 0) // 600_000)


def _block(samples: list[dict[str, Any]], before: dict[str, Any] | None) -> dict[str, Any]:
    """Ten minutes of a run, measured from the sample that closed the previous ten.

    A reading the watch did not send stays null, never zero.
    """
    first, last = before or samples[0], samples[-1]
    seconds = (last["timer_ms"] - first["timer_ms"]) / 1000
    moved = climb = None
    if first.get("distance_m") is not None and last.get("distance_m") is not None:
        moved = last["distance_m"] - first["distance_m"]
    if first.get("altitude_m") is not None and last.get("altitude_m") is not None:
        climb = last["altitude_m"] - first["altitude_m"]
    rates = [s["heart_rate_bpm"] for s in samples if s["heart_rate_bpm"]]
    cadence = [_steps_per_minute(s["cadence_rpm"]) for s in samples if s["cadence_rpm"] and _moving(s)]
    return {
        "from_minute": _block_index(samples[-1]) * 10,
        "average_hr": _rounded(_mean(rates)),
        "average_pace_sec_per_km": _pace(seconds, moved),
        "average_steps_per_minute": _rounded(_mean(cadence)),
        "climb_m": None if climb is None else round(climb, 1),
    }


def _thirds_mean(values: list[float]) -> tuple[float, float] | None:
    if len(values) < 3:
        return None
    size = len(values) // 3
    return sum(values[:size]) / size, sum(values[-size:]) / size


def _thirds(samples: list[dict[str, Any]]) -> tuple[dict[str, Any], dict[str, Any]] | None:
    """First third against last third, each series thirded on its own samples.

    The shape of the provider's run_drift ends, except that cadence is in steps per
    minute as the watch shows it, under its own name.
    """
    if samples[-1]["timer_ms"] - samples[0]["timer_ms"] < MIN_THIRDS_SECONDS * 1000:
        return None
    first: dict[str, Any] = {}
    last: dict[str, Any] = {}
    for key, values, convert in (
        ("average_hr", [s["heart_rate_bpm"] for s in samples if s["heart_rate_bpm"]], round),
        ("average_pace_sec_per_km", [s["speed_mps"] for s in samples if s["speed_mps"]],
         lambda speed: round(1000 / speed)),
        ("average_steps_per_minute",
         [_steps_per_minute(s["cadence_rpm"]) for s in samples if s["cadence_rpm"]], round),
    ):
        ends = _thirds_mean(values)
        if ends is not None:
            first[key], last[key] = convert(ends[0]), convert(ends[1])
    if "average_hr" not in first:
        return None
    return first, last


def _stretches(samples: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], float]:
    """Where the runner stopped or walked, and the timer seconds spent stopped.

    A paused timer, manual or Auto Pause, shows as wall-clock time between two samples
    that the timer did not count; standing with the timer running shows as speed below
    STOPPED_BELOW_MPS. Each interval between samples takes the kind of the sample that
    ends it, and a reading the watch did not send counts as neither.
    """
    moving_cadence = sorted(
        s["cadence_rpm"] for s in samples
        if s["cadence_rpm"] and s["speed_mps"] is not None and s["speed_mps"] >= STOPPED_BELOW_MPS
    )
    walking_below = None
    if len(moving_cadence) >= 3:
        walking_below = moving_cadence[len(moving_cadence) // 2] * WALKING_CADENCE_SHARE

    def kind(sample: dict[str, Any]) -> str | None:
        speed, cadence = sample["speed_mps"], sample["cadence_rpm"]
        if speed is None:
            return None
        if speed < STOPPED_BELOW_MPS:
            return "stopped"
        if walking_below is not None and cadence and cadence < walking_below:
            return "walking"
        return None

    stretches: list[dict[str, Any]] = []
    stopped_seconds = 0.0
    open_kind, open_at, open_until, open_seconds = None, 0, 0, 0.0

    def extend(what: str | None, start_ms: int, end_ms: int, seconds: float) -> None:
        nonlocal open_kind, open_at, open_until, open_seconds
        if what is not None and what == open_kind and start_ms == open_until:
            open_until, open_seconds = end_ms, open_seconds + seconds
            return
        if open_kind is not None and open_seconds >= MIN_STRETCH_SECONDS:
            stretches.append({"from_minute": round(open_at / 60_000, 1),
                              "seconds": round(open_seconds), "kind": open_kind})
        open_kind, open_at, open_until, open_seconds = what, start_ms, end_ms, seconds

    for before, after in zip(samples, samples[1:]):
        timer = (after["timer_ms"] - before["timer_ms"]) / 1000
        paused = after["observed_at"] - before["observed_at"] - timer
        if paused >= 3:
            extend("stopped", before["timer_ms"], before["timer_ms"], paused)
        what = kind(after) if timer <= MAX_CLASSIFIED_GAP_SECONDS else None
        if what == "stopped":
            stopped_seconds += timer
        extend(what, before["timer_ms"], after["timer_ms"], timer)
    extend(None, 0, 0, 0.0)
    return stretches, stopped_seconds


def summarize_run(samples: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The whole run, from every running sample kept of it.

    Compact, because a client carries every read. Totals are the watch's own: distance, timer, ascent and descent at the last sample.
    Moving time leaves out the timer seconds spent standing, and average pace is over
    moving time.
    """
    if not samples:
        return None
    first, last = samples[0], samples[-1]
    stretches, stopped_seconds = _stretches(samples)
    timer_seconds = last["timer_ms"] / 1000
    moving_seconds = max(timer_seconds - stopped_seconds, 0.0)
    rates = [s["heart_rate_bpm"] for s in samples if s["heart_rate_bpm"]]
    cadence = [_steps_per_minute(s["cadence_rpm"]) for s in samples if s["cadence_rpm"] and _moving(s)]
    blocks: dict[int, list[dict[str, Any]]] = {}
    for sample in samples:
        blocks.setdefault(_block_index(sample), []).append(sample)
    chunks = list(blocks.values())
    thirds = _thirds(samples)
    summary = {
        "started_at": _iso(first["observed_at"] - first["timer_ms"] / 1000),
        "timer_minutes": round(timer_seconds / 60, 1),
        "moving_minutes": round(moving_seconds / 60, 1),
        "distance_km": None if last.get("distance_m") is None else round(last["distance_m"] / 1000, 2),
        "average_pace_sec_per_km": _pace(moving_seconds, last.get("distance_m")),
        "average_hr": _rounded(_mean(rates)),
        "max_hr": max(rates, default=None),
        "average_steps_per_minute": _rounded(_mean(cadence)),
        "ascent_m": _rounded(last.get("ascent_m")),
        "descent_m": _rounded(last.get("descent_m")),
        "first_third": None if thirds is None else thirds[0],
        "last_third": None if thirds is None else thirds[1],
        "stopped_or_walking": stretches,
        "every_10_minutes": [
            _block(chunk, chunks[index - 1][-1] if index else None) for index, chunk in enumerate(chunks)
        ],
    }
    # The watch counts completed workout steps and sends 0 on a run with no workout.
    if last.get("steps_done"):
        summary["workout_steps_done"] = round(last["steps_done"])
    return summary


@dataclass
class _Run:
    sample: dict[str, Any]
    arrival: float
    age_at_arrival: float
    retired: dict[str, float]
    samples: list[dict[str, Any]]


class LiveRunStore:
    """Thread-safe per-process state. Expired owners are evicted on access."""

    def __init__(self, *, clock: Callable[[], float] = time.time,
                 monotonic: Callable[[], float] = time.monotonic, max_owners: int = 1024):
        if type(max_owners) is not int or max_owners < 1:
            raise ValueError("max_owners must be a positive integer")
        self._clock = clock
        self._monotonic = monotonic
        self._max_owners = max_owners
        self._owners: OrderedDict[str, _Run] = OrderedDict()
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
            samples = current.samples if same_session else []
            if sample["state"] == "running" and sample.get("timer_ms") is not None:
                if len(samples) >= MAX_RUN_SAMPLES:
                    del samples[1::2]
                samples.append(sample)
            self._owners[owner_id] = _Run(sample, arrival, max(0, now - observed), retired, samples)
            self._owners.move_to_end(owner_id)
            return {**receipt, "status": "accepted"}

    def recap(self, owner_id: str) -> dict[str, Any] | None:
        """The latest run as the model reads it, or ``None`` when none is held."""
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
            return {
                "source": "garmin_watch",
                "note": NOTE,
                "state": current.sample["state"],
                "last_sample_at": _iso(current.sample["observed_at"]),
                "age_seconds": round(current.age_at_arrival + elapsed),
                **(summarize_run(current.samples) or {}),
            }

    def discard(self, owner_id: str) -> None:
        self._check_owner(owner_id)
        with self._lock:
            self._expire(self._monotonic())
            self._owners.pop(owner_id, None)
