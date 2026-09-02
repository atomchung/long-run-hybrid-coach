"""Reading a file the athlete uploaded into the evidence they already state by talking.

The entry to this product is a conversation. An athlete says "I swam 40 minutes" and it is
stored; an athlete drops in three years of Garmin exports and it should land in the same
place, meaning the same thing, read by the same coach. So there is no import store, no
import router, and no per-format anything below this module: every format here is a
*reader*, and all of them produce the one normalized shape ``athlete_evidence`` already
holds. What survives an import is a summary -- day, sport, duration, distance -- plus
where it came from. The file itself is parsed and dropped (AGENTS.md 2); nothing here can
retain a GPS track because nothing here ever builds one.

**Four readers, one shape.**

- ``csv`` -- the bulk history path. Garmin Connect, Strava and Intervals exports are
  recognised by their own headers; anything else is read through a ``column_mapping`` the
  caller supplies. Both go through the same parse, because "known format" is not a second
  code path, it is a mapping this module already knows.
- ``apple_health_xml`` -- Apple's ``export.xml``, whole or in fragments. It is read
  element by element rather than as a document, so a caller that can only pass the
  ``<Workout>`` elements it filtered out of a 400 MB file gets the identical result as one
  that passes the file. This is the only reader that produces body measurements as well as
  sessions: an Apple export is where an athlete's weight history actually lives.
- ``fit`` -- one activity's own file, base64-encoded, decoded down to its ``session``
  message. A model cannot read these bytes, which is exactly why the decoding is here.
- ``records`` -- rows the caller already extracted, for the sources no code in this
  repository can read: a screenshot, a PDF, a proprietary export. The dedup, the identity
  and the storage are unchanged; only the reading moved.

**Why the numbers are not simply asked of the model.** A model handed a CSV will
transcribe most of it correctly, and this product's whole claim is about the rows it would
not. So the payload crosses the boundary as text and *this* code reads it: what reaches the
store came from the file. ``records`` is the deliberate exception, and it says so in the
provenance of every row it writes.

**Units are never guessed.** A recognised export declares its own units here; an
unrecognised one declares them in the caller's mapping. The one export that states neither
-- Garmin Connect writes a bare ``Distance`` column whose unit follows the account -- gets
its distance dropped with a named reason rather than read as kilometres, and the rest of
the row is imported anyway. A mile read as a kilometre is not a smaller error than a
missing distance; it is the error the coach cannot see (AGENTS.md 3).

**Sports are mapped, never inferred.** ``Run`` is running and
``HKWorkoutActivityTypeCycling`` is cycling: those are spellings of the same word, and a
table of them is data acquisition. A sport this table does not hold is reported back
unmapped, with what the file actually said, so the athlete can name it -- it is not
resolved to the nearest plausible one.

Nothing here scores, compares, or decides. What an imported year of running means is the
coach's reading of it (AGENTS.md 4).
"""

from __future__ import annotations

import base64
import binascii
import csv
import datetime as dt
import hashlib
import io
import re
import struct
from typing import Any, Iterable, Iterator
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .athlete_evidence import DEFAULT_TIMEZONE, REPORTABLE_SPORTS


# The formats a caller may declare. `records` is the escape hatch for a source this
# repository cannot read; it is not a fifth parser, it is the absence of one.
IMPORT_FORMATS = ("csv", "apple_health_xml", "fit", "records")

# How many sessions one upload may carry. A decade of running is a few thousand rows, so
# this is not a limit anybody reaches by importing their history; it is the boundary past
# which a payload is a mistake rather than an export, refused before it is parsed.
MAX_IMPORT_ROWS = 5000


class EvidenceImportError(RuntimeError):
    """A payload could not be read, and nothing was written.

    Raised for the shape of the request -- an unknown format, a mapping naming a column
    the file does not have, base64 that is not base64. A *row* that cannot be read is
    never this: it is reported in ``unreadable`` with its row number and its reason, and
    the rest of the file is imported. One bad row in an eight-year export must not cost
    the athlete the other eight years.
    """


# --------------------------------------------------------------------------------------
# Vocabulary: what other systems call the sports this product knows
# --------------------------------------------------------------------------------------

# Spellings, not judgments. Every entry is a name some export uses for a sport already in
# `REPORTABLE_SPORTS`; nothing here widens what the product can hold. Keys are compared
# lowercased with spaces and underscores stripped, so `Trail Running`, `trail_running` and
# `TrailRunning` are one key.
_SPORT_ALIASES: dict[str, str] = {
    # running
    "run": "running",
    "running": "running",
    "trailrun": "running",
    "trailrunning": "running",
    "treadmillrunning": "running",
    "virtualrun": "running",
    "roadrunning": "running",
    "trackrunning": "running",
    "indoorrunning": "running",
    "hkworkoutactivitytyperunning": "running",
    # cycling
    "ride": "cycling",
    "bike": "cycling",
    "cycling": "cycling",
    "biking": "cycling",
    "roadcycling": "cycling",
    "indoorcycling": "cycling",
    "virtualride": "cycling",
    "mountainbiking": "cycling",
    "gravelcycling": "cycling",
    "hkworkoutactivitytypecycling": "cycling",
    # swimming
    "swim": "swimming",
    "swimming": "swimming",
    "poolswim": "swimming",
    "poolswimming": "swimming",
    "openwaterswim": "swimming",
    "openwaterswimming": "swimming",
    "lapswimming": "swimming",
    "hkworkoutactivitytypeswimming": "swimming",
    # strength
    "strength": "strength",
    "strengthtraining": "strength",
    "weighttraining": "strength",
    "weightlifting": "strength",
    "resistancetraining": "strength",
    "hkworkoutactivitytypetraditionalstrengthtraining": "strength",
    "hkworkoutactivitytypefunctionalstrengthtraining": "strength",
    # hiking
    "hike": "hiking",
    "hiking": "hiking",
    "hkworkoutactivitytypehiking": "hiking",
    # rowing
    "row": "rowing",
    "rowing": "rowing",
    "indoorrowing": "rowing",
    "hkworkoutactivitytyperowing": "rowing",
    # mobility
    "yoga": "mobility",
    "mobility": "mobility",
    "stretching": "mobility",
    "flexibility": "mobility",
    "hkworkoutactivitytypeyoga": "mobility",
    "hkworkoutactivitytypeflexibility": "mobility",
    # recovery
    "recovery": "recovery",
    "hkworkoutactivitytypecooldown": "recovery",
}


def _sport_key(value: str) -> str:
    return re.sub(r"[\s_\-]+", "", value).strip().lower()


def map_sport(value: Any) -> str | None:
    """The product's name for a sport another system named, or ``None`` when unknown.

    ``None`` is reported to the athlete with what the file said, never resolved to the
    nearest plausible sport: a walk stored as a run is a training week that did not happen.
    """
    if not isinstance(value, str) or not value.strip():
        return None
    key = _sport_key(value)
    if key in REPORTABLE_SPORTS:
        return key
    return _SPORT_ALIASES.get(key)


# --------------------------------------------------------------------------------------
# Scalars: durations, distances, days
# --------------------------------------------------------------------------------------

_DURATION_UNITS = {"seconds": 1.0 / 60.0, "minutes": 1.0, "hours": 60.0, "hh:mm:ss": None}
_DISTANCE_UNITS = {"km": 1.0, "m": 0.001, "mi": 1.609344, "miles": 1.609344}


def _duration_minutes(raw: Any, unit: str) -> int:
    """Whole minutes, rounded, from whatever the source counted in.

    Rounded rather than truncated because a 90-second stride drill is a minute of work and
    a zero-minute session is not storable; a session that really rounds to zero is refused
    by the caller's own bound rather than stored as nothing.
    """
    text = str(raw).strip()
    if not text:
        raise ValueError("duration is empty")
    if unit == "hh:mm:ss":
        parts = text.split(":")
        if not 2 <= len(parts) <= 3 or not all(part.strip() for part in parts):
            raise ValueError(f"duration {text!r} is not hh:mm:ss or mm:ss")
        try:
            numbers = [float(part) for part in parts]
        except ValueError as exc:
            raise ValueError(f"duration {text!r} is not hh:mm:ss or mm:ss") from exc
        if len(numbers) == 2:
            numbers = [0.0, *numbers]
        seconds = numbers[0] * 3600 + numbers[1] * 60 + numbers[2]
    else:
        factor = _DURATION_UNITS.get(unit)
        if factor is None:
            raise ValueError(f"duration unit {unit!r} is not one of {', '.join(_DURATION_UNITS)}")
        seconds = float(text.replace(",", "")) * factor * 60.0
    # Half-up rather than round(): Python rounds 62.5 to 62, and an athlete reading
    # 01:02:30 back as 62 minutes would be reading a bug, not a convention.
    minutes = int((seconds + 30) // 60)
    if minutes < 1:
        raise ValueError(f"duration {text!r} is under a minute")
    return minutes


def _distance_km(raw: Any, unit: str | None) -> float | None:
    """Kilometres, or ``None`` when the source did not state a unit this can trust."""
    if unit is None:
        return None
    text = str(raw).strip().replace(",", "")
    if not text or text in {"--", "-", "0", "0.0", "0.00"}:
        return None
    factor = _DISTANCE_UNITS.get(unit)
    if factor is None:
        raise ValueError(f"distance unit {unit!r} is not one of {', '.join(_DISTANCE_UNITS)}")
    value = float(text) * factor
    if value <= 0:
        return None
    return round(value, 3)


_ISO_DATE = re.compile(r"(\d{4})-(\d{2})-(\d{2})")
_SLASH_DATE = re.compile(r"(\d{4})/(\d{1,2})/(\d{1,2})")


def _local_day(raw: Any) -> tuple[str, str | None]:
    """The calendar day a session happened on, and the timestamp it was read from.

    Deliberately naive about zones: every export this reads writes the athlete's *local*
    day already, and re-projecting it through a timezone would move sessions across
    midnight for no gain. The timestamp is kept when the source had one because it is what
    tells two sessions of one sport on one day apart.
    """
    text = str(raw).strip()
    if not text:
        raise ValueError("date is empty")
    match = _ISO_DATE.match(text) or _ISO_DATE.search(text)
    if match:
        day = f"{match.group(1)}-{match.group(2)}-{match.group(3)}"
    else:
        slashed = _SLASH_DATE.match(text) or _SLASH_DATE.search(text)
        if not slashed:
            raise ValueError(f"date {text!r} is not a date this reads")
        day = f"{slashed.group(1)}-{int(slashed.group(2)):02d}-{int(slashed.group(3)):02d}"
    try:
        dt.date.fromisoformat(day)
    except ValueError as exc:
        raise ValueError(f"date {text!r} is not a real date") from exc
    started_at = text if re.search(r"\d{1,2}:\d{2}", text) else None
    return day, started_at


# --------------------------------------------------------------------------------------
# The normalized row every reader produces
# --------------------------------------------------------------------------------------


def _row(
    *,
    index: int,
    day: str,
    sport: str,
    duration_minutes: int,
    distance_km: float | None = None,
    started_at: str | None = None,
    external_id: str | None = None,
    note: str | None = None,
    dropped: tuple[str, ...] = (),
) -> dict[str, Any]:
    return {
        "row": index,
        "date": day,
        "sport": sport,
        "duration_minutes": duration_minutes,
        "distance_km": distance_km,
        "started_at": started_at,
        "external_id": external_id,
        "note": note,
        # Fields this row could have carried and does not, each with the reason. Counted
        # into the report so a silently thinner import is visible as one.
        "dropped": list(dropped),
    }


def _unreadable(index: int, reason: str, saw: str | None = None) -> dict[str, Any]:
    return {"row": index, "reason": reason, "source_value": saw}


class _Reading:
    """What one payload yielded: rows, measurements, and everything it could not read."""

    def __init__(self) -> None:
        self.activities: list[dict[str, Any]] = []
        self.measurements: list[dict[str, Any]] = []
        self.recovery: list[dict[str, Any]] = []
        self.unreadable: list[dict[str, Any]] = []
        # What the file held that this product does not keep, counted by kind rather than
        # listed by row. An Apple Health export carries hundreds of thousands of step,
        # dietary and heart-rate samples; naming each one would bury the sessions the
        # athlete actually asked about. Counting them says the file was read and what was
        # left, which is the part they cannot otherwise tell from a silent drop.
        self.ignored: dict[str, int] = {}

    def ignore(self, kind: str) -> None:
        self.ignored[kind] = self.ignored.get(kind, 0) + 1

    def as_dict(self) -> dict[str, Any]:
        return {
            "activities": self.activities,
            "measurements": self.measurements,
            "recovery": self.recovery,
            "unreadable": self.unreadable,
            "ignored": dict(sorted(self.ignored.items())),
        }


# --------------------------------------------------------------------------------------
# CSV
# --------------------------------------------------------------------------------------

# A recognised export, keyed by columns only that export has. Each entry states its own
# units, which is the whole reason recognising it is worth anything: the alternative is
# asking the caller to restate what the format already fixes.
#
# `distance_unit: None` is not an oversight. Garmin Connect writes a bare `Distance`
# whose unit follows the account's measurement setting and appears nowhere in the file.
# The column is therefore not read, the row imports without it, and the report names why
# -- a caller that knows the account's setting re-sends with an explicit mapping.
_KNOWN_CSV_FORMATS: tuple[dict[str, Any], ...] = (
    {
        "name": "strava",
        "signature": ("Activity ID", "Activity Date", "Activity Type"),
        "date": "Activity Date",
        "sport": "Activity Type",
        "duration": "Elapsed Time",
        "duration_unit": "seconds",
        "distance": "Distance",
        "distance_unit": "km",
        "external_id": "Activity ID",
        "note": "Activity Name",
    },
    {
        "name": "intervals_icu",
        "signature": ("start_date_local", "type", "moving_time"),
        "date": "start_date_local",
        "sport": "type",
        "duration": "moving_time",
        "duration_unit": "seconds",
        "distance": "distance",
        "distance_unit": "m",
        "external_id": "id",
        "note": "name",
    },
    {
        "name": "garmin_connect",
        "signature": ("Activity Type", "Date", "Title"),
        "date": "Date",
        "sport": "Activity Type",
        "duration": "Time",
        "duration_unit": "hh:mm:ss",
        "distance": "Distance",
        "distance_unit": None,
        "external_id": None,
        "note": "Title",
    },
)

_MAPPING_FIELDS = (
    "date",
    "sport",
    "duration",
    "duration_unit",
    "distance",
    "distance_unit",
    "external_id",
    "note",
)


def recognise_csv(header: Iterable[str]) -> dict[str, Any] | None:
    """The built-in mapping for a header this module knows, or ``None``."""
    columns = {str(name).strip() for name in header}
    for known in _KNOWN_CSV_FORMATS:
        if columns.issuperset(known["signature"]):
            return known
    return None


def _validated_mapping(mapping: Any, header: list[str]) -> dict[str, Any]:
    if not isinstance(mapping, dict) or not mapping:
        raise EvidenceImportError("column_mapping must be a non-empty object")
    unknown = sorted(set(mapping) - set(_MAPPING_FIELDS))
    if unknown:
        raise EvidenceImportError(
            f"column_mapping does not take {', '.join(unknown)}; "
            f"it takes {', '.join(_MAPPING_FIELDS)}"
        )
    # No `name`: `recognised_as` answers "which known export was this", and a mapping
    # the caller wrote is by definition not one of them.
    resolved: dict[str, Any] = {"name": None, **{field: None for field in _MAPPING_FIELDS}}
    resolved.update({key: value for key, value in mapping.items() if value is not None})
    for field in ("date", "sport", "duration"):
        if not isinstance(resolved.get(field), str) or not resolved[field].strip():
            raise EvidenceImportError(
                f"column_mapping needs a {field} column; a session cannot be read without one"
            )
    if resolved.get("duration_unit") not in _DURATION_UNITS:
        raise EvidenceImportError(
            "column_mapping needs duration_unit, one of " + ", ".join(_DURATION_UNITS)
        )
    if resolved.get("distance") and resolved.get("distance_unit") not in _DISTANCE_UNITS:
        raise EvidenceImportError(
            "column_mapping names a distance column, so it needs distance_unit, one of "
            + ", ".join(_DISTANCE_UNITS)
        )
    named = [
        resolved[field]
        for field in ("date", "sport", "duration", "distance", "external_id", "note")
        if isinstance(resolved.get(field), str) and resolved[field]
    ]
    missing = sorted({name for name in named if name not in header})
    if missing:
        raise EvidenceImportError(
            f"column_mapping names columns this file does not have: {', '.join(missing)}"
        )
    return resolved


def read_csv(content: str, *, column_mapping: Any = None) -> tuple[_Reading, dict[str, Any]]:
    """Every session in one CSV export, plus the mapping that read it."""
    if not isinstance(content, str) or not content.strip():
        raise EvidenceImportError("content must be non-empty CSV text")
    reader = csv.reader(io.StringIO(content))
    try:
        header = [str(name).strip() for name in next(reader)]
    except StopIteration as exc:
        raise EvidenceImportError("content has no header row") from exc
    # A leading byte-order mark is what a spreadsheet writes, not what the athlete typed.
    if header and header[0].startswith("﻿"):
        header[0] = header[0].lstrip("﻿")
    if column_mapping is not None:
        mapping = _validated_mapping(column_mapping, header)
    else:
        recognised = recognise_csv(header)
        if recognised is None:
            raise EvidenceImportError(
                "this CSV header is not one this reads (Strava, Intervals.icu and Garmin "
                "Connect exports are); send column_mapping naming which columns hold the "
                f"date, sport and duration. The header is: {', '.join(header) or '(empty)'}"
            )
        mapping = recognised

    positions = {
        field: header.index(mapping[field])
        for field in ("date", "sport", "duration", "distance", "external_id", "note")
        if isinstance(mapping.get(field), str) and mapping[field] in header
    }
    reading = _Reading()
    for index, raw in enumerate(reader, start=2):
        if not any(str(cell).strip() for cell in raw):
            continue

        def cell(field: str) -> str | None:
            position = positions.get(field)
            if position is None or position >= len(raw):
                return None
            value = str(raw[position]).strip()
            return value or None

        sport = map_sport(cell("sport"))
        if sport is None:
            reading.unreadable.append(
                _unreadable(index, "sport is not one this product holds", cell("sport"))
            )
            continue
        try:
            day, started_at = _local_day(cell("date"))
        except ValueError as exc:
            reading.unreadable.append(_unreadable(index, str(exc), cell("date")))
            continue
        try:
            minutes = _duration_minutes(cell("duration"), mapping["duration_unit"])
        except (ValueError, TypeError) as exc:
            reading.unreadable.append(_unreadable(index, str(exc), cell("duration")))
            continue
        dropped: list[str] = []
        distance = None
        if "distance" in positions:
            if mapping.get("distance_unit") is None:
                dropped.append(
                    "distance: this export states no unit for its Distance column, so it "
                    "was not read; re-send with column_mapping naming distance_unit"
                )
            else:
                try:
                    distance = _distance_km(cell("distance"), mapping["distance_unit"])
                except (ValueError, TypeError) as exc:
                    dropped.append(f"distance: {exc}")
        reading.activities.append(
            _row(
                index=index,
                day=day,
                sport=sport,
                duration_minutes=minutes,
                distance_km=distance,
                started_at=started_at,
                external_id=cell("external_id"),
                note=cell("note"),
                dropped=tuple(dropped),
            )
        )
    return reading, mapping


# --------------------------------------------------------------------------------------
# Apple Health export.xml
# --------------------------------------------------------------------------------------

# Read element by element rather than as a document, on purpose. A real export.xml runs to
# hundreds of megabytes and no caller is passing one through a tool call; what a caller
# *can* pass is the elements it filtered out of one. Both arrive here as the same thing --
# some XML text containing some elements -- and a fragment is not a truncated file, it is
# the file's own elements with the wrapper missing.
_ELEMENT = re.compile(r"<(Workout|Record)\b([^>]*?)/?>", re.DOTALL)
_ATTRIBUTE = re.compile(r'(\w+)="([^"]*)"')

_HEALTH_MEASUREMENTS = {
    "HKQuantityTypeIdentifierBodyMass": "weight_kg",
    "HKQuantityTypeIdentifierBodyFatPercentage": "body_fat_pct",
}

# Recovery readings an export can be read into the store's own vocabulary without changing
# what the number means. Resting heart rate is one figure a day in beats per minute on
# both sides, so it crosses unchanged.
_HEALTH_RECOVERY = {
    "HKQuantityTypeIdentifierRestingHeartRate": "resting_hr_bpm",
}

# Readings this export carries that are deliberately *not* imported, and the words the
# athlete is told instead of a silent drop. HRV is the one worth stating plainly: Apple
# records SDNN and the provider reports RMSSD, and they are two different measurements of
# one night. Storing Apple's figure under `hrv_last_night_ms` would put both in one series
# under one name, and a coach reading that series would be reading a step that never
# happened in the athlete. Sleep is a second case with a second reason -- Apple records
# per-stage intervals rather than a night's total or a score, so a night has to be
# assembled rather than read, and nothing here assembles evidence.
_HEALTH_DECLINED = {
    "HKQuantityTypeIdentifierHeartRateVariabilitySDNN": (
        "heart rate variability (Apple records SDNN; this coach reads RMSSD, and the two "
        "are different measurements of one night)"
    ),
    # "records", not "nights", and the difference is not cosmetic: Apple writes one row
    # per sleep *stage interval*, so a year is thousands of rows for a few hundred nights.
    # Every other number in this response counts the thing its heading names, and a reader
    # taking 4,200 for nights would be off by an order of magnitude.
    "HKCategoryTypeIdentifierSleepAnalysis": (
        "sleep records (per-stage intervals rather than a night's total or score; several "
        "rows make one night)"
    ),
}


def _health_attributes(raw: str) -> dict[str, str]:
    return {name: value for name, value in _ATTRIBUTE.findall(raw)}


def read_apple_health(content: str) -> _Reading:
    """Sessions and body measurements out of an Apple Health export, whole or partial."""
    if not isinstance(content, str) or not content.strip():
        raise EvidenceImportError("content must be non-empty Apple Health XML text")
    reading = _Reading()
    seen = False
    for index, match in enumerate(_ELEMENT.finditer(content), start=1):
        seen = True
        kind = match.group(1)
        attributes = _health_attributes(match.group(2))
        if kind == "Workout":
            _read_health_workout(reading, index, attributes)
        else:
            _read_health_record(reading, index, attributes)
    if not seen:
        raise EvidenceImportError(
            "no <Workout> or <Record> elements were found; an Apple Health export is "
            "read from those elements, and a fragment containing them reads the same as "
            "the whole file"
        )
    return reading


def _read_health_workout(reading: _Reading, index: int, attributes: dict[str, str]) -> None:
    sport = map_sport(attributes.get("workoutActivityType"))
    if sport is None:
        reading.unreadable.append(
            _unreadable(
                index,
                "workoutActivityType is not a sport this product holds",
                attributes.get("workoutActivityType"),
            )
        )
        return
    try:
        day, started_at = _local_day(attributes.get("startDate"))
    except ValueError as exc:
        reading.unreadable.append(_unreadable(index, str(exc), attributes.get("startDate")))
        return
    # Apple writes the unit beside the number, which is why this reader never has to be
    # told one. `duration` is minutes by default and says so in `durationUnit`.
    unit = (attributes.get("durationUnit") or "min").strip().lower()
    scale = {"min": 1.0, "s": 1.0 / 60.0, "sec": 1.0 / 60.0, "hr": 60.0}.get(unit)
    if scale is None:
        reading.unreadable.append(_unreadable(index, "durationUnit is not one this reads", unit))
        return
    try:
        seconds = float(attributes.get("duration", "")) * scale * 60.0
        minutes = int((seconds + 30) // 60)
    except (TypeError, ValueError) as exc:
        reading.unreadable.append(_unreadable(index, str(exc), attributes.get("duration")))
        return
    if minutes < 1:
        reading.unreadable.append(
            _unreadable(index, "duration is under a minute", attributes.get("duration"))
        )
        return
    distance = None
    dropped: list[str] = []
    raw_distance = attributes.get("totalDistance")
    if raw_distance:
        distance_unit = (attributes.get("totalDistanceUnit") or "km").strip().lower()
        try:
            distance = _distance_km(raw_distance, distance_unit)
        except (ValueError, TypeError) as exc:
            dropped.append(f"distance: {exc}")
    reading.activities.append(
        _row(
            index=index,
            day=day,
            sport=sport,
            duration_minutes=minutes,
            distance_km=distance,
            started_at=started_at,
            # Apple gives no stable per-workout id in the export, so identity falls back
            # to the content fingerprint every reader shares.
            external_id=None,
            dropped=tuple(dropped),
        )
    )


def _read_health_recovery(
    reading: _Reading, index: int, field: str, attributes: dict[str, str]
) -> None:
    """One recovery reading, dated by the day it was taken in the export's own local time."""
    try:
        day, _ = _local_day(attributes.get("startDate"))
    except ValueError as exc:
        reading.unreadable.append(_unreadable(index, str(exc), attributes.get("startDate")))
        return
    try:
        value = float(attributes.get("value", ""))
    except (TypeError, ValueError) as exc:
        reading.unreadable.append(_unreadable(index, str(exc), attributes.get("value")))
        return
    reading.recovery.append({"date": day, field: value})


def _read_health_record(reading: _Reading, index: int, attributes: dict[str, str]) -> None:
    record_type = attributes.get("type", "")
    recovery_field = _HEALTH_RECOVERY.get(record_type)
    if recovery_field is not None:
        _read_health_recovery(reading, index, recovery_field, attributes)
        return
    declined = _HEALTH_DECLINED.get(record_type)
    if declined is not None:
        # Counted by name, because these two are the ones an athlete uploading a health
        # export expects to have been read. Saying so is the whole difference between a
        # decision and a silent drop.
        reading.ignore(declined)
        return
    field = _HEALTH_MEASUREMENTS.get(record_type)
    if field is None:
        # Every other Record type in an export -- per-beat heart rate, steps, dietary
        # anything -- is not a measurement this product holds. Reporting each as
        # unreadable would bury one real problem under a hundred thousand non-problems,
        # so they are counted under one heading instead of named: the athlete is told
        # their file held other readings and that none of them were kept, without being
        # handed a hundred thousand rows to read.
        reading.ignore("other readings this coach does not keep")
        return
    try:
        day, _ = _local_day(attributes.get("startDate"))
    except ValueError as exc:
        reading.unreadable.append(_unreadable(index, str(exc), attributes.get("startDate")))
        return
    try:
        value = float(attributes.get("value", ""))
    except (TypeError, ValueError) as exc:
        reading.unreadable.append(_unreadable(index, str(exc), attributes.get("value")))
        return
    unit = (attributes.get("unit") or "").strip().lower()
    if field == "weight_kg":
        if unit in {"lb", "lbs"}:
            value = value * 0.45359237
        elif unit not in {"kg", ""}:
            reading.unreadable.append(_unreadable(index, "body mass unit is not kg or lb", unit))
            return
    else:
        # Apple stores body fat as a fraction with unit `%`; both spellings appear.
        value = value * 100 if value <= 1 else value
    reading.measurements.append(
        {"row": index, "date": day, "field": field, "value": round(value, 2)}
    )


# --------------------------------------------------------------------------------------
# FIT
# --------------------------------------------------------------------------------------

# Enough of the FIT binary format to read one activity's summary, and no more. The file is
# a header, a stream of definition and data messages, and a CRC; a definition says which
# fields a later data message carries and how wide each one is, which is what makes the
# stream readable without the full message profile. Only two messages are read out of it:
# `file_id` (0), for an identity that survives a re-upload, and `session` (18), which is
# the summary this product stores anyway.
_FIT_HEADER = struct.Struct("<BBHI4s")
_FIT_EPOCH = dt.datetime(1989, 12, 31, tzinfo=dt.timezone.utc)
_FIT_FILE_ID = 0
_FIT_SESSION = 18
# The `activity` message, read for one thing: it is the only place a FIT file states the
# local time its own timestamps correspond to. Everything else in the format is UTC, and
# an evening run in Taipei is UTC the same day while a 6 a.m. one is UTC the day before --
# so a session dated off the raw timestamp lands a day early for exactly the sessions
# this athlete does most. `local_timestamp` minus `timestamp` is the device's own offset
# at the moment of recording, which is the answer, and it is the device's rather than a
# guess about where the athlete was that morning.
_FIT_ACTIVITY = 34

# size in bytes, struct code, and the value that means "this field was not recorded".
_FIT_BASE_TYPES: dict[int, tuple[int, str, int | None]] = {
    0x00: (1, "B", 0xFF),  # enum
    0x01: (1, "b", 0x7F),
    0x02: (1, "B", 0xFF),
    0x83: (2, "h", 0x7FFF),
    0x84: (2, "H", 0xFFFF),
    0x85: (4, "i", 0x7FFFFFFF),
    0x86: (4, "I", 0xFFFFFFFF),
    0x07: (1, "s", None),  # string
    0x88: (4, "f", None),
    0x89: (8, "d", None),
    0x0A: (1, "B", 0x00),  # uint8z
    0x8B: (2, "H", 0x0000),
    0x8C: (4, "I", 0x00000000),
    0x0D: (1, "B", 0xFF),  # byte
    0x8E: (8, "q", 0x7FFFFFFFFFFFFFFF),
    0x8F: (8, "Q", 0xFFFFFFFFFFFFFFFF),
    0x90: (8, "Q", 0x0000000000000000),
}

# The FIT sport enum, for the sports this product holds. `training` (10) is only a
# strength session when its sub_sport says so, which is why it is not in this table.
_FIT_SPORTS = {1: "running", 2: "cycling", 5: "swimming", 15: "rowing", 17: "hiking"}
_FIT_STRENGTH_SUB_SPORT = 20


def _fit_messages(payload: bytes) -> Iterator[tuple[int, dict[int, Any]]]:
    """Every data message in one FIT file, as ``(global message number, fields)``."""
    if len(payload) < 14:
        raise EvidenceImportError("this is too short to be a FIT file")
    header_size, _protocol, _profile, data_size, magic = _FIT_HEADER.unpack_from(payload, 0)
    if magic != b".FIT" or header_size not in (12, 14):
        raise EvidenceImportError("this does not carry a FIT file header")
    cursor = header_size
    end = min(len(payload), header_size + data_size)
    definitions: dict[int, dict[str, Any]] = {}
    while cursor < end:
        record_header = payload[cursor]
        cursor += 1
        if record_header & 0x80:
            # Compressed timestamp header: a data message whose local type is in bits 5-6.
            local_type = (record_header >> 5) & 0x03
            definition = definitions.get(local_type)
            if definition is None:
                raise EvidenceImportError("a FIT data message arrived before its definition")
            fields, cursor = _fit_read_fields(payload, cursor, definition)
            yield definition["global"], fields
            continue
        local_type = record_header & 0x0F
        if record_header & 0x40:
            definition, cursor = _fit_definition(payload, cursor, bool(record_header & 0x20))
            definitions[local_type] = definition
            continue
        definition = definitions.get(local_type)
        if definition is None:
            raise EvidenceImportError("a FIT data message arrived before its definition")
        fields, cursor = _fit_read_fields(payload, cursor, definition)
        yield definition["global"], fields


def _fit_definition(payload: bytes, cursor: int, developer: bool) -> tuple[dict[str, Any], int]:
    if cursor + 5 > len(payload):
        raise EvidenceImportError("a FIT definition message is truncated")
    architecture = payload[cursor + 1]
    order = ">" if architecture == 1 else "<"
    (global_number,) = struct.unpack_from(f"{order}H", payload, cursor + 2)
    field_count = payload[cursor + 4]
    cursor += 5
    fields: list[tuple[int, int, int]] = []
    for _ in range(field_count):
        if cursor + 3 > len(payload):
            raise EvidenceImportError("a FIT definition message is truncated")
        fields.append((payload[cursor], payload[cursor + 1], payload[cursor + 2]))
        cursor += 3
    if developer:
        if cursor >= len(payload):
            raise EvidenceImportError("a FIT definition message is truncated")
        developer_count = payload[cursor]
        cursor += 1
        for _ in range(developer_count):
            if cursor + 3 > len(payload):
                raise EvidenceImportError("a FIT definition message is truncated")
            # Developer fields are read only for their width: they carry an application's
            # own data, never a session summary, and skipping them by size is what keeps
            # the stream aligned for the messages that do matter.
            fields.append((-1, payload[cursor + 1], payload[cursor + 2]))
            cursor += 3
    return {"global": global_number, "order": order, "fields": fields}, cursor


def _fit_read_fields(
    payload: bytes, cursor: int, definition: dict[str, Any]
) -> tuple[dict[int, Any], int]:
    order = definition["order"]
    values: dict[int, Any] = {}
    for number, size, base_type in definition["fields"]:
        if cursor + size > len(payload):
            raise EvidenceImportError("a FIT data message is truncated")
        raw = payload[cursor : cursor + size]
        cursor += size
        if number < 0:
            continue
        spec = _FIT_BASE_TYPES.get(base_type)
        if spec is None:
            continue
        width, code, invalid = spec
        if code == "s":
            text = raw.split(b"\x00")[0].decode("utf-8", "replace")
            values[number] = text or None
            continue
        if size != width:
            # An array field. Only its first element is ever a summary value here.
            if size < width:
                continue
            raw = raw[:width]
        (value,) = struct.unpack(f"{order}{code}", raw)
        values[number] = None if invalid is not None and value == invalid else value
    return values, cursor


def read_fit(content: str, *, timezone_name: str) -> _Reading:
    """The one session inside one base64-encoded FIT file, on the day it was trained.

    ``timezone_name`` is the athlete's own stored timezone, used only when the file states
    no offset of its own -- a fallback to a fact this product already holds, never an
    assumption. A file that does state one is believed over it: the device was there.
    """
    if not isinstance(content, str) or not content.strip():
        raise EvidenceImportError("content must be a base64-encoded FIT file")
    try:
        payload = base64.b64decode(content, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise EvidenceImportError(f"content is not valid base64: {exc}") from exc

    reading = _Reading()
    serial: int | None = None
    created: int | None = None
    offset: dt.timedelta | None = None
    sessions: list[dict[int, Any]] = []
    for global_number, fields in _fit_messages(payload):
        if global_number == _FIT_FILE_ID:
            serial = serial if serial is not None else fields.get(3)
            created = created if created is not None else fields.get(4)
        elif global_number == _FIT_SESSION:
            sessions.append(fields)
        elif global_number == _FIT_ACTIVITY and offset is None:
            local, utc = fields.get(5), fields.get(253)
            if isinstance(local, int) and isinstance(utc, int):
                offset = dt.timedelta(seconds=local - utc)
    zone = _zone_or_utc(timezone_name) if offset is None else dt.timezone(offset)
    if not sessions:
        raise EvidenceImportError(
            "this FIT file holds no session summary; only a completed activity carries one"
        )

    identity = "-".join(str(part) for part in (serial, created) if part is not None)
    for index, fields in enumerate(sessions, start=1):
        sport_number = fields.get(5)
        sub_sport = fields.get(6)
        if sport_number == 10 and sub_sport == _FIT_STRENGTH_SUB_SPORT:
            sport = "strength"
        else:
            sport = _FIT_SPORTS.get(sport_number) if isinstance(sport_number, int) else None
        if sport is None:
            reading.unreadable.append(
                _unreadable(index, "the FIT sport is not one this product holds", str(sport_number))
            )
            continue
        start = fields.get(2) if isinstance(fields.get(2), int) else fields.get(253)
        if not isinstance(start, int):
            reading.unreadable.append(_unreadable(index, "this session states no start time"))
            continue
        moment = (_FIT_EPOCH + dt.timedelta(seconds=start)).astimezone(zone)
        seconds = fields.get(8) if fields.get(8) is not None else fields.get(7)
        if not isinstance(seconds, int):
            reading.unreadable.append(_unreadable(index, "this session states no duration"))
            continue
        minutes = int((seconds / 1000.0 + 30) // 60)
        if minutes < 1:
            reading.unreadable.append(_unreadable(index, "duration is under a minute"))
            continue
        raw_distance = fields.get(9)
        distance = (
            round(raw_distance / 100.0 / 1000.0, 3)
            if isinstance(raw_distance, int) and raw_distance > 0
            else None
        )
        reading.activities.append(
            _row(
                index=index,
                day=moment.date().isoformat(),
                sport=sport,
                duration_minutes=minutes,
                distance_km=distance,
                started_at=moment.isoformat(),
                external_id=f"fit-{identity}-{index}" if identity else None,
            )
        )
    return reading


# --------------------------------------------------------------------------------------
# Rows the caller already read
# --------------------------------------------------------------------------------------


def _zone_or_utc(timezone_name: str) -> dt.tzinfo:
    """The athlete's own zone, falling back to UTC only when it cannot be resolved.

    An unresolvable name is not worth refusing an upload over -- every other reader here
    takes local time straight from the file, so this is the one place a zone is consulted
    at all, and UTC at least dates the session somewhere real.
    """
    try:
        return ZoneInfo(timezone_name)
    except (ZoneInfoNotFoundError, ValueError):
        return dt.timezone.utc


def read_records(records: Any) -> _Reading:
    """Sessions the caller extracted from a source no reader here can open.

    Everything downstream is identical -- the same normalization, the same dedup, the same
    storage -- which is the point: this is a missing *reader*, not a second import. The
    provenance says ``records`` so the coach can tell a row a parser produced from one a
    model transcribed.
    """
    if not isinstance(records, list) or not records:
        raise EvidenceImportError("records must be a non-empty array of sessions")
    reading = _Reading()
    for index, record in enumerate(records, start=1):
        if not isinstance(record, dict):
            reading.unreadable.append(_unreadable(index, "each record must be an object"))
            continue
        unknown = sorted(
            set(record) - {"date", "sport", "duration_minutes", "distance_km", "started_at",
                           "external_id", "note"}
        )
        if unknown:
            reading.unreadable.append(
                _unreadable(index, f"unknown field(s): {', '.join(unknown)}")
            )
            continue
        sport = map_sport(record.get("sport"))
        if sport is None:
            reading.unreadable.append(
                _unreadable(index, "sport is not one this product holds", str(record.get("sport")))
            )
            continue
        try:
            day, from_date = _local_day(record.get("date"))
        except ValueError as exc:
            reading.unreadable.append(_unreadable(index, str(exc), str(record.get("date"))))
            continue
        minutes = record.get("duration_minutes")
        if isinstance(minutes, bool) or not isinstance(minutes, int) or minutes < 1:
            reading.unreadable.append(
                _unreadable(index, "duration_minutes must be a whole number of minutes",
                            str(minutes))
            )
            continue
        distance = record.get("distance_km")
        if distance is not None and (
            isinstance(distance, bool) or not isinstance(distance, (int, float)) or distance <= 0
        ):
            reading.unreadable.append(
                _unreadable(index, "distance_km must be a positive number or absent", str(distance))
            )
            continue
        started_at = record.get("started_at") or from_date
        note = record.get("note")
        reading.activities.append(
            _row(
                index=index,
                day=day,
                sport=sport,
                duration_minutes=minutes,
                distance_km=round(float(distance), 3) if distance is not None else None,
                started_at=str(started_at) if started_at else None,
                external_id=(
                    str(record["external_id"]) if record.get("external_id") is not None else None
                ),
                note=note if isinstance(note, str) and note.strip() else None,
            )
        )
    return reading


# --------------------------------------------------------------------------------------
# One entry, four readers
# --------------------------------------------------------------------------------------


def payload_digest(format_name: str, content: str | None, records: Any) -> str:
    """The identity of the upload itself, so re-sending one is visibly the same upload.

    Over the payload as it arrived, not over what was parsed: the athlete dragging the
    same file in twice is the case this answers, and that is a fact about the file.
    """
    material = content if isinstance(content, str) else repr(records)
    return hashlib.sha256(f"{format_name}\n{material}".encode("utf-8")).hexdigest()


def read_payload(
    *,
    format_name: Any,
    content: Any = None,
    records: Any = None,
    column_mapping: Any = None,
    timezone_name: str = DEFAULT_TIMEZONE,
) -> dict[str, Any]:
    """Read one upload into normalized rows. Writes nothing and reaches nothing."""
    if format_name not in IMPORT_FORMATS:
        raise EvidenceImportError(
            f"format must be one of {', '.join(IMPORT_FORMATS)}, found {format_name!r}"
        )
    if format_name == "records":
        if content is not None:
            raise EvidenceImportError("format 'records' takes records, not content")
        reading = read_records(records)
        mapping = None
    else:
        if records is not None:
            raise EvidenceImportError(f"format {format_name!r} takes content, not records")
        if column_mapping is not None and format_name != "csv":
            raise EvidenceImportError("column_mapping applies to csv only")
        if format_name == "csv":
            reading, mapping = read_csv(content, column_mapping=column_mapping)
        elif format_name == "apple_health_xml":
            reading, mapping = read_apple_health(content), None
        else:
            reading, mapping = read_fit(content, timezone_name=timezone_name), None
    return {
        **reading.as_dict(),
        "format": format_name,
        "recognised_as": mapping.get("name") if mapping else None,
        "digest": payload_digest(format_name, content if isinstance(content, str) else None, records),
    }
