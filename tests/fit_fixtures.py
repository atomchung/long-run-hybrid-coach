"""Synthetic FIT payloads for tests that must not commit a real activity file.

AGENTS.md 2 keeps FIT activities out of this repository, so a test that needs the
set reader to be exercised builds its bytes here instead of loading a fixture.
"""

from __future__ import annotations

import struct


def fit_file_without_sets() -> bytes:
    """A structurally valid FIT file carrying no records at all.

    What a strength activity started but never stepped through produces: the reader
    walks it, finds no set messages, and answers "no sets" rather than raising. That
    is a different fact from a file that could not be parsed, and the two must stay
    distinguishable (AGENTS.md 3). The trailing two bytes are the file CRC slot; the
    reader bounds its walk by the header's declared data size and never reads them.
    """
    header = struct.pack("<BBHI4sH", 14, 0x20, 2140, 0, b".FIT", 0)
    return header + struct.pack("<H", 0)


# A heart-rate series far shorter than the reader's fifteen-minute floor, so drift
# reports nothing for it. A test that is actually about drift supplies its own.
STREAMS_TOO_SHORT_FOR_DRIFT = [{"type": "heartrate", "data": [130, 131, 132]}]


def fit_file_with_sets(sets: list[tuple[int, int]]) -> bytes:
    """A FIT file carrying one ``set`` message per ``(duration_ms, set_type)`` pair.

    ``set_type`` is 1 for a working set and 0 for the rest after it, which is how the
    device tells them apart. Built here rather than committed as a captured file for
    the reason at the top of this module.
    """
    # One definition message declaring the two fields the reader looks at, then one
    # data message per set. Field 0 is duration (uint32), field 5 is set_type (enum).
    definition = (
        bytes([0x40, 0x00, 0x00])
        + struct.pack("<H", 225)
        + bytes([2, 0, 4, 0x86, 5, 1, 0x00])
    )
    records = b"".join(
        bytes([0x00]) + struct.pack("<I", duration) + bytes([set_type])
        for duration, set_type in sets
    )
    body = definition + records
    header = struct.pack("<BBHI4sH", 14, 0x20, 2140, len(body), b".FIT", 0)
    return header + body + struct.pack("<H", 0)


def hr_stream(samples: list[int]) -> list[dict]:
    return [{"type": "heartrate", "data": samples}]


def drift_streams(**series: list[float]) -> list[dict]:
    """Streams keyed the way the provider returns them, for a drift test."""
    return [{"type": name, "data": data} for name, data in series.items()]
