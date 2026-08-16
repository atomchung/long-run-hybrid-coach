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

Which language it renders in is the athlete's, not the deployment's. Two are supported --
Traditional Chinese and English -- and they are two tables of words behind one code path,
not a translation framework: every sentence this module can produce is built from the same
plan fields in the same order, so a language is a vocabulary and nothing more. Movement
names are not in either table; ``display_name`` is already the athlete's own word for the
lift and is carried through untouched.

Both avoid the decorative separators the watch's font drops (``｜``, ``·``) so the same
wording can be read on the phone and on the device.

``pace_text`` and ``duration_text`` live here rather than in ``delivery`` because both
renderings need them and there must be one implementation of "how a pace is spelled".
"""

from __future__ import annotations

from typing import Any


# The languages a prescription can be written in, and the one an athlete who has never
# said otherwise gets. The default is Traditional Chinese because that is what every
# stored plan was rendered in before a language could be stated; changing it would rewrite
# the meaning of every plan already on disk.
DEFAULT_LANGUAGE = "zh-Hant"
LANGUAGES = ("zh-Hant", "en")

# The two vocabularies. Every entry is either a plain word or a format string over values
# the plan already holds -- there is no per-field translation layer, and adding a third
# language would be adding a third entry here and nothing else.
#
# The English side is written for the two places it actually lands: an Intervals event
# description and a strength day's calendar title, both of which reach the athlete's watch
# through the sync chain. So it stays short and unpunctuated, the same shape the Chinese
# side has.
_WORDS: dict[str, dict[str, str]] = {
    "zh-Hant": {
        # What an unstructured session says, in full. It is a constant on purpose: the
        # model declares no duration, no target and no load, so there is nothing to render
        # and nothing a sentence could smuggle in. What the session is *for* is `purpose`.
        "unstructured": "不設定量化目標",
        # What an absent load means, said in words rather than left blank -- blank is how
        # "bodyweight" and "we have not measured this yet" became the same thing.
        "bodyweight": "自重",
        "pending_confirmation": "待確認",
        "distance_km": "{value}公里",
        "distance_m": "{value}公尺",
        "duration_min_sec": "{minutes}分{seconds}秒",
        "duration_min": "{minutes}分",
        "duration_sec": "{seconds}秒",
        "pace": " 配速 {pace}/km",
        "pace_range": " 配速 {low}-{high}/km",
        "hr_ceiling": " 心率上限 {bpm} bpm",
        "repeat": "{count}趟：{steps}",
        "repeat_join": "、",
        "failure_sets": "{sets}組力竭",
        "load_kg": "{value}公斤",
        "assist_kg": "輔助{value}公斤",
        # A watch title has less room than the full prescription's sentence, so load is
        # spelled in the short unit there. Only the unit differs between the two.
        "title_load_kg": "{value}kg",
        "title_assist_kg": "輔助{value}kg",
        "title_separator": "：",
    },
    "en": {
        "unstructured": "No quantified target",
        "bodyweight": "bodyweight",
        "pending_confirmation": "load TBD",
        "distance_km": "{value} km",
        "distance_m": "{value} m",
        "duration_min_sec": "{minutes} min {seconds} s",
        "duration_min": "{minutes} min",
        "duration_sec": "{seconds} s",
        "pace": " pace {pace}/km",
        "pace_range": " pace {low}-{high}/km",
        "hr_ceiling": " HR cap {bpm} bpm",
        "repeat": "{count} rounds: {steps}",
        "repeat_join": ", ",
        "failure_sets": "{sets} sets to failure",
        "load_kg": "{value} kg",
        "assist_kg": "assist {value} kg",
        "title_load_kg": "{value}kg",
        "title_assist_kg": "assist {value}kg",
        "title_separator": ": ",
    },
}


# The only two ``load_basis`` values a movement may carry, so a plan naming something else
# renders no load word rather than reaching into the table for one that happens to match.
_LOAD_BASIS_KEYS = frozenset({"bodyweight", "pending_confirmation"})


def normalize_language(language: Any) -> str:
    """The language to render in, falling back rather than raising.

    A value this module has no words for renders in the default, because a prescription is
    an output on a path that has already accepted the plan: refusing here would block a
    valid session over the wording of one field. The write paths that *store* a language
    refuse an unsupported one, which is where that refusal belongs.
    """
    return language if language in _WORDS else DEFAULT_LANGUAGE


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


def _duration_words(duration: Any, words: dict[str, str]) -> str:
    if not isinstance(duration, dict):
        return ""
    if duration.get("kind") == "distance":
        meters = int(duration.get("meters", 0))
        if meters % 1000 == 0:
            return words["distance_km"].format(value=meters // 1000)
        return words["distance_m"].format(value=meters)
    seconds = int(duration.get("seconds", 0))
    minutes, remainder = divmod(seconds, 60)
    if minutes and remainder:
        return words["duration_min_sec"].format(minutes=minutes, seconds=remainder)
    if minutes:
        return words["duration_min"].format(minutes=minutes)
    return words["duration_sec"].format(seconds=remainder)


def _target_words(target: Any, words: dict[str, str]) -> str:
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
            return words["pace"].format(pace=pace_text(low))
        return words["pace_range"].format(low=pace_text(low), high=pace_text(high))
    if kind == "hr_ceiling":
        # The plan says the ceiling in bpm, which is what the athlete trains to. The
        # delivered workout text says the same ceiling as a percentage of threshold HR,
        # because that is the only form Intervals can carry to the watch -- the delivery
        # preview shows both, so the two are never read as two different instructions.
        return words["hr_ceiling"].format(bpm=target.get("ceiling_bpm"))
    return ""


def _work_text(step: dict[str, Any], words: dict[str, str]) -> str:
    name = str(step.get("name") or "").strip()
    parts = [part for part in (name, _duration_words(step.get("duration"), words)) if part]
    return " ".join(parts) + _target_words(step.get("target"), words)


def _time_axis_text(plan: dict[str, Any], words: dict[str, str]) -> str:
    """One line per top-level step; a repeat inlines its own steps behind its count."""
    lines: list[str] = []
    steps = plan.get("steps")
    for step in steps if isinstance(steps, list) else []:
        if not isinstance(step, dict):
            continue
        if step.get("kind") == "repeat":
            children = step.get("steps")
            inner = words["repeat_join"].join(
                _work_text(child, words)
                for child in (children if isinstance(children, list) else [])
                if isinstance(child, dict)
            )
            lines.append(
                words["repeat"].format(count=step.get("repetitions"), steps=inner)
            )
            continue
        lines.append(_work_text(step, words))
    return "\n".join(lines)


def _scheme_text(sets: Any, reps: Any, words: dict[str, str]) -> str:
    """`5x5`, or the words for a set with no rep count -- one taken to failure by design.

    Shared with the delivered title (`strength_title_suffix`): a failure set reads the
    same way whether the athlete is looking at the full prescription or the watch title,
    and only the load's unit differs between those two renderings.
    """
    return f"{sets}x{reps}" if reps is not None else words["failure_sets"].format(sets=sets)


def _load_text(
    movement: dict[str, Any], words: dict[str, str], *, load_key: str, assist_key: str
) -> str:
    """The load a movement carries, or what stands in its place when it carries none."""
    loads: list[str] = []
    if movement.get("load_kg") is not None:
        loads.append(words[load_key].format(value=_number_text(movement["load_kg"])))
    if movement.get("assist_kg") is not None:
        loads.append(words[assist_key].format(value=_number_text(movement["assist_kg"])))
    if loads:
        return " ".join(loads)
    basis = str(movement.get("load_basis"))
    return words[basis] if basis in _LOAD_BASIS_KEYS else ""


def _movement_text(movement: dict[str, Any], words: dict[str, str]) -> str:
    # `display_name`, never `exercise`: the latter is the canonical key the evidence gate
    # matches on ("back_squat"), and it would reach the athlete's first screen and the
    # watch's calendar entry as an internal identifier. The schema requires the name, so
    # there is no fallback to get wrong.
    exercise = str(movement.get("display_name") or "").strip()
    scheme = _scheme_text(movement.get("sets"), movement.get("reps"), words)
    loads = _load_text(movement, words, load_key="load_kg", assist_key="assist_kg")
    return " ".join(part for part in (exercise, scheme, loads) if part)


def _movement_list_text(plan: dict[str, Any], words: dict[str, str]) -> str:
    movements = plan.get("movements")
    return "\n".join(
        _movement_text(movement, words)
        for movement in (movements if isinstance(movements, list) else [])
        if isinstance(movement, dict)
    )


def render_prescription(plan: Any, language: str = DEFAULT_LANGUAGE) -> str:
    """The athlete-readable rendering of one ``session.plan``, in their own language.

    Dispatch is on ``kind`` and only on ``kind``: the execution model decides how a
    session reads, exactly as it decides which validation runs. A sport this product
    has not met yet reuses whichever model fits it and renders with no change here.
    """
    words = _WORDS[normalize_language(language)]
    if not isinstance(plan, dict):
        return words["unstructured"]
    kind = plan.get("kind")
    if kind == "time_axis":
        return _time_axis_text(plan, words)
    if kind == "movement_list":
        return _movement_list_text(plan, words)
    return words["unstructured"]


def language_of(plan: Any, prescription: Any) -> str:
    """Which language a session's stored sentence was written in.

    Read off the artifact rather than passed alongside it, because the two things that
    have to agree are already both in the session: the sentence a delivered event
    describes itself with, and the title above it. A session validated by this product
    holds a sentence this module produced, so exactly one rendering matches -- and when
    a plan happens to read the same way in both languages there is nothing to tell apart.

    The default answers a sentence that matches neither, which validation would already
    have refused; delivery never sees one.
    """
    for language in LANGUAGES:
        if render_prescription(plan, language) == prescription:
            return language
    return DEFAULT_LANGUAGE


def strength_title(purpose: str, plan: Any, language: str = DEFAULT_LANGUAGE) -> str:
    """The whole name a delivered strength calendar entry carries.

    ``purpose`` is the athlete's own sentence about the day and is never translated;
    only the separator and the suffix behind it come from the language table.
    """
    suffix = strength_title_suffix(plan, language)
    if not suffix:
        return purpose
    return f"{purpose}{_WORDS[normalize_language(language)]['title_separator']}{suffix}"


def strength_title_suffix(plan: Any, language: str = DEFAULT_LANGUAGE) -> str | None:
    """The primary lift and its load, the way a delivered strength title states them.

    ``None`` for anything that is not a movement list -- ``unstructured`` (movements
    declined) or a malformed plan -- so the caller falls back to the bare purpose that
    already titled the entry, the same fallback ``render_prescription`` takes to the
    unstructured sentence.

    The primary lift is the first entry in ``plan.movements``: plan order is the only
    ranking a movement list carries, so there is no separate field to add and nothing
    left for the model to guess at. The load figure it renders already passed the same
    baseline-anchor validation the full prescription reads it from.

    A watch title has less room than the full prescription's sentence, so this spells
    load in the short unit -- only the number formatting (``_number_text``) is shared
    with the full rendering, not the unit word; see module docstring.
    """
    if not isinstance(plan, dict) or plan.get("kind") != "movement_list":
        return None
    movements = plan.get("movements")
    if not isinstance(movements, list) or not movements:
        return None
    primary = movements[0]
    if not isinstance(primary, dict):
        return None

    words = _WORDS[normalize_language(language)]
    exercise = str(primary.get("display_name") or "").strip()
    scheme = _scheme_text(primary.get("sets"), primary.get("reps"), words)
    loads = _load_text(primary, words, load_key="title_load_kg", assist_key="title_assist_kg")
    return " ".join(part for part in (exercise, scheme, loads) if part) or None
