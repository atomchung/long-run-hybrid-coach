"""Render one session's plan as the sentence the athlete reads.

``prescription`` used to be an input: the Coach wrote a sentence, and deterministic
code read its numbers back out with eleven regular expressions. Every defect that
layer produced had the same shape -- a correct plan rejected because its wording was
unreadable, or an unanchored number accepted because the wording hid it.

Here prose is an output. ``session.plan`` is the only source, this module is the only
writer, and a rendering cannot disagree with what it renders. That is what makes
deleting the text checks safe rather than reckless: there is no second statement of the
session for the structure to contradict.

Two properties the callers depend on:

- **Pure.** ``render_prescription`` reads the plan object and nothing else, so two
  sessions with the same structure always produce the same sentence, in any order, on
  any machine. ``validation`` compares a stored prescription against this function's
  output, which is what makes an authored one impossible to store.
- **Total.** It renders whatever a validated plan can hold and never raises on one.
  Callers validate the plan first; malformed input is the validator's error to report,
  not this module's to guess at.

The text is Traditional Chinese because that is the athlete's language, and it avoids
the decorative separators the watch's font drops (``｜``, ``·``) so the same wording can
be read on the phone and on the device.

``pace_text`` and ``duration_text`` live here rather than in ``delivery`` because both
renderings need them and there must be one implementation of "how a pace is spelled".
"""

from __future__ import annotations

from typing import Any


# What an unstructured session says, in full. It is a constant on purpose: the model
# declares no duration, no target and no load, so there is nothing to render and nothing
# a sentence could smuggle in. What the session is *for* is `purpose`, beside this.
UNSTRUCTURED_TEXT = "不設定量化目標"

# What an absent load means, said in the athlete's language rather than left blank --
# blank is how "bodyweight" and "we have not measured this yet" became the same thing.
LOAD_BASIS_TEXT = {
    "bodyweight": "自重",
    "pending_confirmation": "待確認",
}


def pace_text(seconds: int) -> str:
    """A pace in M:SS. Shared with the provider description so one pace reads one way."""
    minutes, remainder = divmod(int(seconds), 60)
    return f"{minutes}:{remainder:02d}"


def duration_text(duration: dict[str, Any]) -> str:
    """Provider workout-text duration: ``10m``, ``1km``, ``500mtr``, ``1m30s``.

    This is Intervals' own syntax, not athlete-facing prose; the Chinese form below is
    a separate rendering because a step the watch parses and a sentence a person reads
    are not the same text.
    """
    if duration["kind"] == "distance":
        meters = duration["meters"]
        return f"{meters // 1000}km" if meters % 1000 == 0 else f"{meters}mtr"
    seconds = duration["seconds"]
    minutes, remainder = divmod(seconds, 60)
    if minutes and remainder:
        return f"{minutes}m{remainder}s"
    return f"{minutes}m" if minutes else f"{remainder}s"


def _number_text(value: Any) -> str:
    """A load as the athlete would write it: 70, not 70.0; 62.5 stays 62.5."""
    number = float(value)
    return str(int(number)) if number.is_integer() else str(number)


def _chinese_duration(duration: Any) -> str:
    if not isinstance(duration, dict):
        return ""
    if duration.get("kind") == "distance":
        meters = int(duration.get("meters", 0))
        return f"{meters // 1000}公里" if meters % 1000 == 0 else f"{meters}公尺"
    seconds = int(duration.get("seconds", 0))
    minutes, remainder = divmod(seconds, 60)
    if minutes and remainder:
        return f"{minutes}分{remainder}秒"
    return f"{minutes}分" if minutes else f"{remainder}秒"


def _chinese_target(target: Any) -> str:
    """The intensity a step binds, or nothing at all for an open one.

    An `open` target renders as silence rather than as a word: the plan deliberately
    left the intensity to the athlete, and naming that would read as an instruction the
    session does not give.
    """
    if not isinstance(target, dict):
        return ""
    kind = target.get("kind")
    if kind == "pace":
        low, high = target.get("low_seconds_per_km"), target.get("high_seconds_per_km")
        if low == high:
            return f" 配速 {pace_text(low)}/km"
        return f" 配速 {pace_text(low)}-{pace_text(high)}/km"
    if kind == "hr_ceiling":
        # The plan says the ceiling in bpm, which is what the athlete trains to. The
        # delivered workout text says the same ceiling as a percentage of threshold HR,
        # because that is the only form Intervals can carry to the watch -- the delivery
        # preview shows both, so the two are never read as two different instructions.
        return f" 心率上限 {target.get('ceiling_bpm')} bpm"
    return ""


def _work_text(step: dict[str, Any]) -> str:
    name = str(step.get("name") or "").strip()
    parts = [part for part in (name, _chinese_duration(step.get("duration"))) if part]
    return " ".join(parts) + _chinese_target(step.get("target"))


def _time_axis_text(plan: dict[str, Any]) -> str:
    """One line per top-level step; a repeat inlines its own steps behind its count."""
    lines: list[str] = []
    steps = plan.get("steps")
    for step in steps if isinstance(steps, list) else []:
        if not isinstance(step, dict):
            continue
        if step.get("kind") == "repeat":
            children = step.get("steps")
            inner = "、".join(
                _work_text(child)
                for child in (children if isinstance(children, list) else [])
                if isinstance(child, dict)
            )
            lines.append(f"{step.get('repetitions')}趟：{inner}")
            continue
        lines.append(_work_text(step))
    return "\n".join(lines)


def _scheme_text(sets: Any, reps: Any) -> str:
    """`5x5`, or `5組力竭` for a set with no rep count -- one taken to failure by design.

    Shared with the delivered title (`strength_title_suffix`): a failure set reads the
    same way whether the athlete is looking at the full prescription or the watch title,
    and only the load's unit differs between those two renderings.
    """
    return f"{sets}x{reps}" if reps is not None else f"{sets}組力竭"


def _movement_text(movement: dict[str, Any]) -> str:
    # `display_name`, never `exercise`: the latter is the canonical key the evidence gate
    # matches on ("back_squat"), and it would reach the athlete's first screen and the
    # watch's calendar entry as an internal identifier. The schema requires the name, so
    # there is no fallback to get wrong.
    exercise = str(movement.get("display_name") or "").strip()
    scheme = _scheme_text(movement.get("sets"), movement.get("reps"))

    loads: list[str] = []
    if movement.get("load_kg") is not None:
        loads.append(f"{_number_text(movement['load_kg'])}公斤")
    if movement.get("assist_kg") is not None:
        loads.append(f"輔助{_number_text(movement['assist_kg'])}公斤")
    if not loads:
        basis = LOAD_BASIS_TEXT.get(str(movement.get("load_basis")))
        if basis:
            loads.append(basis)
    return " ".join(part for part in (exercise, scheme, " ".join(loads)) if part)


def _movement_list_text(plan: dict[str, Any]) -> str:
    movements = plan.get("movements")
    return "\n".join(
        _movement_text(movement)
        for movement in (movements if isinstance(movements, list) else [])
        if isinstance(movement, dict)
    )


def render_prescription(plan: Any) -> str:
    """The athlete-readable rendering of one ``session.plan``.

    Dispatch is on ``kind`` and only on ``kind``: the execution model decides how a
    session reads, exactly as it decides which validation runs. A sport this product
    has not met yet reuses whichever model fits it and renders with no change here.
    """
    if not isinstance(plan, dict):
        return UNSTRUCTURED_TEXT
    kind = plan.get("kind")
    if kind == "time_axis":
        return _time_axis_text(plan)
    if kind == "movement_list":
        return _movement_list_text(plan)
    return UNSTRUCTURED_TEXT


def strength_title_suffix(plan: Any) -> str | None:
    """The primary lift and its load, the way a delivered strength title states them.

    ``None`` for anything that is not a movement list -- ``unstructured`` (movements
    declined) or a malformed plan -- so the caller falls back to the bare purpose that
    already titled the entry, the same fallback ``render_prescription`` takes to
    ``UNSTRUCTURED_TEXT``.

    The primary lift is the first entry in ``plan.movements``: plan order is the only
    ranking a movement list carries, so there is no separate field to add and nothing
    left for the model to guess at. The load figure it renders already passed the same
    baseline-anchor validation the full prescription reads it from.

    A watch title has less room than the full prescription's sentence, so this spells
    load in ``kg`` rather than ``公斤`` -- only the number formatting (``_number_text``)
    is shared with that Chinese rendering, not the unit word; see module docstring.
    """
    if not isinstance(plan, dict) or plan.get("kind") != "movement_list":
        return None
    movements = plan.get("movements")
    if not isinstance(movements, list) or not movements:
        return None
    primary = movements[0]
    if not isinstance(primary, dict):
        return None

    exercise = str(primary.get("display_name") or "").strip()
    scheme = _scheme_text(primary.get("sets"), primary.get("reps"))

    loads: list[str] = []
    if primary.get("load_kg") is not None:
        loads.append(f"{_number_text(primary['load_kg'])}kg")
    if primary.get("assist_kg") is not None:
        loads.append(f"輔助{_number_text(primary['assist_kg'])}kg")
    if not loads:
        basis = LOAD_BASIS_TEXT.get(str(primary.get("load_basis")))
        if basis:
            loads.append(basis)
    return " ".join(part for part in (exercise, scheme, " ".join(loads)) if part) or None
