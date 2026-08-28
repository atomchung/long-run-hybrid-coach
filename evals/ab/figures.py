"""Which figures an answer states, and which of them the context actually handed it.

This is the one mechanical thing worth computing about a coaching answer. A coach that
can no longer read what a session prescribed has exactly three ways out -- say so, ask
the athlete, or state a number anyway -- and only the third is silent. So the check that
matters is whether every pace, load, distance, duration and heart rate in the answer is
one the context carries.

Deliberately conservative in both directions, because a signal that cries wolf gets
ignored and then it is not a signal:

* figures are matched on the **number**, not on the number and its unit. ``70`` in the
  answer is supported by ``70`` anywhere in the context, whatever either meant. That
  under-reports invention and never over-reports it.
* the athlete's own question is part of the supported set. A number they said is a
  number the coach may repeat.
* what comes out is a *list*, not a verdict. A model that says "比上週多了 12%" states a
  figure the context does not carry and has invented nothing; the reviewer reads the
  list and decides. Nothing here scores.

The derivations below exist because one reading is spelled two ways. A pace stored as
``average_pace_sec_per_km: 333`` is read aloud as ``5:33``; a distance stored as
``meters: 8000`` is read as ``8``公里. Without them every correctly-cited pace in every
answer would be reported as unsupported, which is the failure mode that makes a check
like this worthless. The same two derivations apply to a ``segment_rows`` entry -- a
row is a bare positional list rather than a dict with its own keys, so it is read
against its activity's own ``segment_fields`` declaration to learn which position is
which, not against a second, parallel set of rules.
"""

from __future__ import annotations

import json
import re
from typing import Any, Iterable


# A number wearing a unit. The units are the ones this product's answers are made of --
# a bare number is not a figure, because "第 3 週" and "3 公里" are not the same claim.
#
# Longest alternative first, always: with ``m`` before ``min`` the regex reads "12 min"
# as twelve metres, which is the same digits attached to a different claim.
_UNIT = r"公斤|公里|公尺|分鐘|分|秒|趟|組|次|下|bpm|kg|km|min|%|m|s"
_FIGURE_WITH_UNIT = re.compile(rf"(\d+(?:\.\d+)?)\s*(?:{_UNIT})", re.IGNORECASE)
# A pace or a clock duration: 5:33, 6:00, 1:45.
_CLOCK = re.compile(r"\b(\d{1,2}):([0-5]\d)\b")
# A set scheme: 5x5, 4X6, 3×10.
_SCHEME = re.compile(r"\b(\d+)\s*[x×X]\s*(\d+)\b")
# Dates and ISO timestamps carry digits that are not figures. Removed before matching.
_DATELIKE = re.compile(r"\d{4}-\d{2}-\d{2}(?:T[\d:.+-]+)?")
# The keys whose value is spelled a second way when a person says it out loud.
_PACE_KEYS = ("_sec_per_km", "_seconds_per_km")
_SECOND_KEYS = ("seconds", "_sec", "_secs")
_METER_KEYS = ("meters", "_m")


def _number_text(value: float) -> str:
    """``70.0`` and ``70`` are one figure. ``62.5`` stays ``62.5``."""
    return str(int(value)) if float(value).is_integer() else str(float(value))


def _clock_text(seconds: float) -> str:
    minutes, remainder = divmod(int(seconds), 60)
    return f"{minutes}:{remainder:02d}"


def figures_in_text(text: str) -> set[str]:
    """Every figure a piece of prose states, normalized to its number alone."""
    scrubbed = _DATELIKE.sub(" ", text or "")
    found: set[str] = set()
    for match in _FIGURE_WITH_UNIT.finditer(scrubbed):
        found.add(_number_text(float(match.group(1))))
    for match in _CLOCK.finditer(scrubbed):
        found.add(f"{int(match.group(1))}:{match.group(2)}")
    for match in _SCHEME.finditer(scrubbed):
        found.add(_number_text(float(match.group(1))))
        found.add(_number_text(float(match.group(2))))
    return found


def _walk(node: Any, key: str, into: set[str]) -> None:
    if isinstance(node, dict):
        for name, value in node.items():
            _walk(value, str(name), into)
        # A `segment_execution` activity past the full-detail window carries
        # `segment_rows` instead of `segments` -- one row per segment, but a bare
        # positional list (``["WORK", 1000.0, 358]``) rather than a dict with its own
        # keys. The generic recursion above still visits every cell, under the parent
        # key `segment_rows`, which is why the raw numbers land in `into` already; but
        # `segment_rows` matches none of the suffixes below, so the *_sec/meters
        # derivations that fire for the dict `segments` shape never fire here. The
        # activity's own `segment_fields` names what each position means, so re-walking
        # each cell under its real field name reaches the same derivations the dict
        # shape gets -- adding to `into`, never replacing what the pass above already
        # found.
        if isinstance(node.get("segment_rows"), list) and isinstance(
            node.get("segment_fields"), list
        ):
            _walk_segment_rows(node["segment_fields"], node["segment_rows"], into)
        return
    if isinstance(node, list):
        for value in node:
            _walk(value, key, into)
        return
    if isinstance(node, bool) or node is None:
        return
    if isinstance(node, (int, float)):
        into.add(_number_text(node))
        lowered = key.lower()
        if any(lowered.endswith(suffix) for suffix in _PACE_KEYS):
            into.add(_clock_text(node))
        elif any(lowered.endswith(suffix) for suffix in _SECOND_KEYS):
            into.add(_clock_text(node))
            if node % 60 == 0:
                into.add(_number_text(node / 60))
        elif any(lowered.endswith(suffix) for suffix in _METER_KEYS):
            if node % 1000 == 0:
                into.add(_number_text(node / 1000))
        return
    if isinstance(node, str):
        into.update(figures_in_text(node))


def _walk_segment_rows(fields: list[Any], rows: list[Any], into: set[str]) -> None:
    """Re-walk compact ``segment_rows`` cells under their declared field names.

    Each row is positional -- ``fields[i]`` names what ``row[i]`` is, the same order
    ``segment_fields`` was written in. Calling ``_walk`` again per cell, keyed by that
    name instead of by the enclosing list's key, is what lets a ``moving_time_sec``
    cell reach the ``*_sec`` clock derivation and a ``distance_m`` cell reach the
    meters-to-km derivation -- the exact branches in ``_walk`` above, not a second copy
    of them. A row shorter or longer than ``fields`` is walked only up to whichever
    runs out first; nothing here reads across rows, so no sum or difference between
    segments is invented.
    """
    for row in rows:
        if not isinstance(row, list):
            continue
        for position, value in enumerate(row):
            if position >= len(fields):
                break
            _walk(value, str(fields[position]), into)


def supported_figures(*payloads: Any) -> set[str]:
    """Every figure the coach was handed, across everything it was handed."""
    into: set[str] = set()
    for payload in payloads:
        _walk(payload, "", into)
    return into


def unsupported_figures(answer: str, supported: Iterable[str]) -> list[str]:
    """The figures in an answer that nothing the coach was handed carries.

    Sorted numerically where possible so a report reads in a stable order rather than
    in whatever order a set happened to iterate.
    """
    remaining = figures_in_text(answer) - set(supported)

    def sort_key(figure: str) -> tuple[int, float, str]:
        if ":" in figure:
            minutes, _, seconds = figure.partition(":")
            return (1, int(minutes) * 60 + int(seconds), figure)
        return (0, float(figure), figure)

    return sorted(remaining, key=sort_key)


def json_text(value: Any) -> str:
    """The compact JSON a size measurement is taken over."""
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False)
