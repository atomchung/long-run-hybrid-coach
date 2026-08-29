"""Read the per-set structure of a strength session out of a Garmin FIT file.

Intervals stores the file a watch uploaded but parses none of its ``set``
messages: the activity endpoint returns no exercise, no set count, no reps and
no load, ``icu_lap_count`` is 0, and the FIT it generates on download has the
set messages stripped. Probed live on 2026-08-28 against the real account: the
generated file for one session carried 0 set messages at 15 KB, while the
*original* upload carried 48 at 114 KB. So the only path to what happened
inside a strength session is the original file, read here.

What this module returns is a summary of a handful of integers, never the file.
The bytes are parsed and dropped (AGENTS.md 2): no timestamps of individual
sets, no exercise identity, nothing that reconstructs the session.

Two fields the watch does record are deliberately not carried out of here:

``weight``
    Absent from every set message on the real account, because it is only
    written when the athlete types it in on the watch. Absent is not zero
    (AGENTS.md 3), and the athlete's own statement through
    ``recordStrengthExecution`` is the load of record either way.

``category``
    An array of the watch's guesses, not an answer. One real set came back
    ``[push_up, triceps_extension, pull_up]`` and two whole sets came back
    ``unknown``. Reporting a guess as an exercise name would be inventing
    precision the device does not have.

``repetitions`` is recorded and readable, and is left out for the same reason:
on a session the athlete logged as five-by-five, the watch's own counts
included a 7, a 9 and a 0. What the coach reads for reps is what the athlete
said.

What survives is the shape of the session in time, which no other source has
and the athlete never volunteers: how many working sets, how long under load
against how long in the gym, and whether rest and set length drifted across the
session.
"""

from __future__ import annotations

import struct
from typing import Any

# FIT global message number for a strength set. Both a working set and the rest
# after it are ``set`` messages, told apart by ``set_type``.
SET_MESSAGE = 225

# Field numbers within a set message (FIT Profile).
FIELD_DURATION = 0
FIELD_SET_TYPE = 5

SET_TYPE_REST = 0
SET_TYPE_ACTIVE = 1

# Base type sizes, indexed by the low 5 bits of a field's FIT base type.
_BASE_TYPES: dict[int, tuple[str, int]] = {
    0: ("B", 1), 1: ("b", 1), 2: ("B", 1), 3: ("h", 2), 4: ("H", 2),
    5: ("i", 4), 6: ("I", 4), 7: ("s", 0), 8: ("f", 4), 9: ("d", 8),
    10: ("B", 1), 11: ("H", 2), 12: ("I", 4), 13: ("B", 1),
    14: ("q", 8), 15: ("Q", 8), 16: ("Q", 8),
}

_HEADER_DEFINITION = 0x40
_HEADER_DEVELOPER = 0x20
_HEADER_COMPRESSED = 0x80

# A duration is milliseconds. A set the watch left open reads as the field's
# invalid marker rather than as a number, and is dropped rather than read as 0.
_INVALID_UINT32 = 0xFFFFFFFF


class FitParseError(ValueError):
    """The bytes are not a FIT file this reader can walk.

    Raised rather than returning ``None`` so a caller can tell "this file could
    not be read" from "this file held no sets", which are different facts about
    the session (AGENTS.md 3).
    """


def _read_field(
    raw: bytes, offset: int, size: int, base_type: int, endian: str
) -> Any:
    kind = base_type & 0x1F
    fmt, width = _BASE_TYPES.get(kind, ("B", 1))
    if kind == 7 or width == 0 or size % width:
        return None
    values = [
        struct.unpack_from(endian + fmt, raw, offset + index * width)[0]
        for index in range(size // width)
    ]
    return values[0] if len(values) == 1 else values


def _iter_set_messages(payload: bytes) -> list[dict[int, Any]]:
    """Walk the record stream and return the parsed ``set`` messages in order.

    A FIT file interleaves definition messages, which declare a local message
    type's fields and their widths, with data messages that carry values in that
    declared order. Every message must be walked even when it is not a set,
    because a data message's length is only knowable from its definition.
    """
    if len(payload) < 14:
        raise FitParseError("file shorter than a FIT header")
    header_size = payload[0]
    if header_size not in (12, 14) or payload[8:12] != b".FIT":
        raise FitParseError("missing FIT signature")
    data_size = struct.unpack_from("<I", payload, 4)[0]
    position = header_size
    end = header_size + data_size
    if end > len(payload):
        raise FitParseError("declared data size exceeds the file")

    definitions: dict[int, dict[str, Any]] = {}
    messages: list[dict[int, Any]] = []

    while position < end:
        header = payload[position]
        position += 1
        if header & _HEADER_COMPRESSED:
            local = (header >> 5) & 0x03
            definition = definitions.get(local)
            if definition is None:
                raise FitParseError("data message before its definition")
            position += definition["size"]
            continue
        local = header & 0x0F
        if header & _HEADER_DEFINITION:
            position += 1  # reserved
            endian = ">" if payload[position] else "<"
            position += 1
            global_number = struct.unpack_from(endian + "H", payload, position)[0]
            position += 2
            field_count = payload[position]
            position += 1
            fields: list[tuple[int, int, int]] = []
            for _ in range(field_count):
                fields.append(
                    (payload[position], payload[position + 1], payload[position + 2])
                )
                position += 3
            if header & _HEADER_DEVELOPER:
                developer_count = payload[position]
                position += 1
                for _ in range(developer_count):
                    # Developer fields carry no number this reader understands;
                    # only their width matters, to keep the walk aligned.
                    fields.append((-1, payload[position + 1], payload[position + 2]))
                    position += 3
            definitions[local] = {
                "global": global_number,
                "fields": fields,
                "size": sum(field[1] for field in fields),
                "endian": endian,
            }
            continue
        definition = definitions.get(local)
        if definition is None:
            raise FitParseError("data message before its definition")
        raw = payload[position : position + definition["size"]]
        if definition["global"] == SET_MESSAGE:
            parsed: dict[int, Any] = {}
            offset = 0
            for number, size, base_type in definition["fields"]:
                if number >= 0:
                    parsed[number] = _read_field(
                        raw, offset, size, base_type, definition["endian"]
                    )
                offset += size
            messages.append(parsed)
        position += definition["size"]

    return messages


def _mean_seconds(durations_ms: list[int]) -> int | None:
    if not durations_ms:
        return None
    return round(sum(durations_ms) / len(durations_ms) / 1000)


def _thirds(durations_ms: list[int]) -> tuple[int | None, int | None]:
    """Mean of the first and last third, or ``(None, None)`` when too short.

    Three is the smallest count where a first and last third are disjoint. Below
    that the two ends would share a value and any drift read off them would be an
    artefact of the arithmetic rather than of the session.
    """
    if len(durations_ms) < 3:
        return None, None
    size = len(durations_ms) // 3
    return _mean_seconds(durations_ms[:size]), _mean_seconds(durations_ms[-size:])


def summarise_sets(payload: bytes) -> dict[str, Any] | None:
    """Summarise one strength session's set structure, or ``None`` if it has none.

    ``None`` is the honest answer for a session the watch recorded without sets --
    a strength activity started but never stepped through, or a sport whose file
    carries no set messages at all. It is not an error, and it is a different
    fact from a file that could not be parsed, which raises instead.
    """
    messages = _iter_set_messages(payload)
    work: list[int] = []
    rest: list[int] = []
    for message in messages:
        duration = message.get(FIELD_DURATION)
        if not isinstance(duration, int) or duration == _INVALID_UINT32:
            continue
        set_type = message.get(FIELD_SET_TYPE)
        if set_type == SET_TYPE_ACTIVE:
            work.append(duration)
        elif set_type == SET_TYPE_REST:
            rest.append(duration)

    if not work:
        return None

    under_load_ms = sum(work)
    rest_first, rest_last = _thirds(rest)
    set_first, set_last = _thirds(work)
    return {
        "work_sets": len(work),
        "under_load_sec": round(under_load_ms / 1000),
        "recorded_sec": round((under_load_ms + sum(rest)) / 1000),
        "rest_first_third_sec": rest_first,
        "rest_last_third_sec": rest_last,
        "set_first_third_sec": set_first,
        "set_last_third_sec": set_last,
    }
