"""Model Context Protocol transport over the Coach Gateway's own routes.

An MCP client -- claude.ai's custom connector, Codex, any other -- speaks JSON-RPC 2.0
over one HTTP endpoint instead of one path per operation. That is the only difference
this module introduces. Every tool below ends in ``CoachGateway.route``, which is where
coaching capability lives and is deliberately not this module: the product holds exactly
one validator, one store, one delivery boundary and one identity check, and a new entry
changes data sources and operator tooling, never coaching capability (AGENTS.md
invariant 10). ``CoachGateway.route_kinds`` is the list this catalogue is held against.

Three consequences shape the code:

- **Nothing here touches product state.** This module imports no store, delivery or
  validation module and holds no owner, token or path. It reads a JSON-RPC message,
  names a route kind, and renders whatever the gateway handed back. The one thing it
  does own outright is ``prompts``: the orchestration layer in ``orchestration.md``,
  which every entry needs (issue #125).
- **The gateway owns identity; this module owns the protocol.** The bearer token is
  resolved to an owner before this module sees a byte, so a message from an unknown
  token never reaches the parser here.
- **A refused coaching action is a tool result, not a protocol failure.** The model has
  to read a block -- a stale plan version, a missing confirmation, an open delivery
  reservation -- and act on it, which it cannot do if the transport folded it into a
  JSON-RPC error the client handles instead. Only a message that cannot be read as a
  request at all becomes a JSON-RPC error object.

The server is stateless: no ``Mcp-Session-Id`` is issued, nothing is remembered between
requests, and no SSE stream is opened. Streamable HTTP permits all three, and a
restarted process therefore loses nothing a client needs.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable, Sequence

from . import orchestration
from .athlete_evidence import IMPORT_RESOLUTIONS
from .evidence_import import IMPORT_FORMATS
from .release_identity import sha256_text


# The revision of the MCP specification this server implements. Anything else a client
# asks for is answered with this one (see ``_negotiated_version``): the tool surface is
# version-independent, but JSON-RPC batching -- which 2025-03-26 allows and this server
# refuses -- is not, so agreeing to an older version would promise something untrue.
PROTOCOL_VERSION = "2025-06-18"
SUPPORTED_PROTOCOL_VERSIONS = (PROTOCOL_VERSION,)

# What the `MCP-Protocol-Version` HTTP header may say, which is a wider set than the one
# above and deliberately so. The header is not the handshake: it states which revision an
# already-negotiated connection is speaking, and 2025-06-18 requires a server that
# receives no header at all to assume 2025-03-26. Refusing that value in the header while
# assuming it in its absence would refuse exactly the clients the spec accommodates.
HTTP_PROTOCOL_VERSIONS: tuple[str, ...] = (PROTOCOL_VERSION, "2025-03-26")

SERVER_NAME = "garmin-coach-loop"

# JSON-RPC 2.0 error codes. Only these four can occur here: everything past the protocol
# layer is a coaching answer, including a refusal.
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602


class ToolCallBlocked(Exception):
    """One tool call the gateway refused, carrying the gateway's own error payload.

    Raised by the caller's ``call_tool``, not here: the gateway owns what a refusal
    says, this module owns only where it lands in the response.
    """

    def __init__(self, payload: dict[str, Any]):
        super().__init__(str(payload.get("error") or "blocked"))
        self.payload = payload


# --------------------------------------------------------------------------------------
# Tool input schemas
#
# Static, self-contained JSON Schema, and the only description of the command surface.
# MCP gives each tool its own `inputSchema` with no way to $ref a shape another tool
# declares, so a grammar two tools both accept is written out in both -- the one kind of
# duplication this file cannot factor out.
# --------------------------------------------------------------------------------------


_WORKOUT_DURATION: dict[str, Any] = {
    "type": "object",
    "required": ["kind"],
    "properties": {
        "kind": {"type": "string", "enum": ["time", "distance"]},
        "seconds": {"type": "integer", "description": "Required when kind is time."},
        "meters": {"type": "integer", "description": "Required when kind is distance."},
    },
}

_WORKOUT_TARGET: dict[str, Any] = {
    "type": "object",
    "description": (
        "open leaves intensity to the athlete; pace needs a measured threshold pace; "
        "hr_ceiling is an upper bound only and needs a measured max heart rate."
    ),
    "required": ["kind"],
    "properties": {
        "kind": {"type": "string", "enum": ["open", "pace", "hr_ceiling"]},
        "unit": {"type": "string", "enum": ["sec_per_km", "bpm"]},
        "low_seconds_per_km": {"type": "integer"},
        "high_seconds_per_km": {"type": "integer"},
        "ceiling_bpm": {"type": "integer"},
    },
}

_WORKOUT_BLOCK: dict[str, Any] = {
    "type": "object",
    "description": "One work step, or one repeat wrapping work steps.",
    "required": ["kind"],
    "properties": {
        "kind": {"type": "string", "enum": ["work", "repeat"]},
        "name": {"type": "string", "description": "Work steps only."},
        "duration": _WORKOUT_DURATION,
        "target": _WORKOUT_TARGET,
        "repetitions": {"type": "integer", "description": "Repeat blocks only."},
        "steps": {
            "type": "array",
            "description": (
                "Repeat blocks only; work steps, never another repeat. A heart-rate "
                "ceiling cannot be used inside a repeat."
            ),
            "items": {
                "type": "object",
                "required": ["kind", "name", "duration", "target"],
                "properties": {
                    "kind": {"type": "string", "enum": ["work"]},
                    "name": {"type": "string"},
                    "duration": _WORKOUT_DURATION,
                    "target": _WORKOUT_TARGET,
                },
            },
        },
    },
}

_MEASUREMENT: dict[str, Any] = {
    "type": "object",
    "description": (
        "The runnable half of the protocol: which ordinary session this cycle measures against, which of its weeks repeats that session, and what to hold constant. Optional -- a protocol in prose alone is still a protocol, and a review then says the measurement is owed rather than reading one that was never taken."
    ),
    "required": ["reference_session_id", "measurement_week_start", "compare"],
    "properties": {
        "reference_session_id": {
            "type": "string",
            "description": (
                "The session_id this cycle measures against -- an ordinary session, "
                "usually early in the cycle, whose result is the reading everything is "
                "compared to. Not a new kind of session."
            ),
        },
        "measurement_week_start": {
            "type": "string",
            "description": (
                "ISO date of the week that repeats it, and one of this cycle's own "
                "weeks. Scheduling that session when the week arrives is your job at "
                "the review, not something the athlete is expected to remember."
            ),
        },
        "compare": {
            "type": "string",
            "description": (
                "What is held constant and what is read: \"same route and pace, compare "
                "average heart rate\". State the comparison; the verdict is yours to "
                "give at the review, and nothing scores it."
            ),
        },
    },
}


_OUTLOOK_WEEK: dict[str, Any] = {
    "type": "object",
    "description": (
        "One outlined week. It carries no session ids and no prescriptions, so nothing "
        "here can be delivered to a calendar or reconciled -- a week becomes precise "
        "when a review makes it the current week."
    ),
    "required": ["week_start", "intent", "key_sessions", "relation_to_primary"],
    "properties": {
        "week_start": {
            "type": "string",
            "description": "ISO date, exactly seven days after the week before it.",
        },
        "intent": {"type": "string", "description": "What this week is for."},
        "key_sessions": {
            "type": "array",
            "minItems": 1,
            "items": {"type": "string"},
            "description": (
                "The sessions that give the week its shape, as the athlete would "
                "recognise them: what kind, and roughly how much. State magnitude, not "
                "a pace, heart rate or load no anchor supports yet."
            ),
        },
        "relation_to_primary": {
            "type": "string",
            "description": (
                "How this week moves the primary adaptation -- build, hold, recover, or "
                "measure."
            ),
        },
    },
}


_STRENGTH_MOVEMENT: dict[str, Any] = {
    "type": "object",
    "description": (
        "One planned movement. load_basis says why a load is what it is, so an absent "
        "kg figure is never a guess."
    ),
    "required": [
        "exercise",
        "display_name",
        "sets",
        "reps",
        "load_kg",
        "assist_kg",
        "load_basis",
    ],
    "properties": {
        "exercise": {
            "type": "string",
            "description": (
                "The canonical key this lift's baseline uses, so the two compare field "
                "to field. Never shown to the athlete."
            ),
        },
        "display_name": {
            "type": "string",
            "description": (
                "The movement as the athlete reads it, in their own language. This is "
                "the name that reaches their screen and the watch's calendar entry, so "
                "give it even when it matches the key."
            ),
        },
        "sets": {"type": "integer", "minimum": 1},
        "reps": {
            "type": ["integer", "null"],
            "description": "Null for a set taken to failure.",
        },
        "load_kg": {"type": ["number", "null"]},
        "assist_kg": {"type": ["number", "null"]},
        "load_basis": {
            "type": "string",
            "enum": ["measured_baseline", "bodyweight", "pending_confirmation"],
            "description": (
                "measured_baseline needs a load_kg or assist_kg figure matching a "
                "baseline strength_load; bodyweight and pending_confirmation must "
                "leave both null."
            ),
        },
    },
}

_SESSION_PLAN: dict[str, Any] = {
    "description": (
        "What the session prescribes, structured, classified by how it is executed "
        "rather than by which sport it is: time_axis for work laid out along time or "
        "distance with an intensity target, movement_list for work laid out as "
        "movements with sets and loads, unstructured for a session that declares no "
        "numbers at all (mobility, recovery, rest -- and a strength session whose "
        "athlete declined to enumerate movements, which adopts with a warning; a run "
        "may never be unstructured). No tool here accepts a prescription sentence: it "
        "is rendered from this object and returned in the preview, so it cannot say "
        "something this object does not."
    ),
    "oneOf": [
        {
            "type": "object",
            "description": (
                "What the watch executes for a running session. One target kind per "
                "session -- never mix pace and heart rate."
            ),
            "required": ["kind", "name", "steps"],
            "properties": {
                "kind": {"type": "string", "enum": ["time_axis"]},
                "name": {
                    "type": "string",
                    "description": (
                        "At most 80 characters; the title the athlete sees on the watch."
                    ),
                },
                "steps": {"type": "array", "minItems": 1, "items": _WORKOUT_BLOCK},
            },
        },
        {
            "type": "object",
            "description": (
                "What the athlete lifts, so the plan's numbers can be compared with "
                "what came back. It never reaches the watch as structure -- a movement "
                "list publishes as a titled calendar entry."
            ),
            "required": ["kind", "movements"],
            "properties": {
                "kind": {"type": "string", "enum": ["movement_list"]},
                "movements": {"type": "array", "minItems": 1, "items": _STRENGTH_MOVEMENT},
            },
        },
        {
            "type": "object",
            "description": (
                "A session that declares no numbers -- mobility, recovery, rest. It "
                "carries no field but its own kind, so nothing can ride along inside it."
            ),
            "required": ["kind"],
            "properties": {"kind": {"type": "string", "enum": ["unstructured"]}},
        },
    ],
}

_FALLBACK: dict[str, Any] = {
    "type": "object",
    "required": ["action", "description"],
    "properties": {
        "action": {"type": "string", "enum": ["reduce", "move", "replace", "rest"]},
        "description": {"type": "string"},
    },
}

_ADAPTATIONS = [
    "aerobic_base",
    "threshold",
    "vo2",
    "strength",
    "hypertrophy",
    "power",
    "recovery",
]
# Named from the modules that own them rather than re-listed, for the same reason the
# sport list below is: a second copy is a second answer, and the one in the catalogue is
# the one a model would believe.
_IMPORT_FORMATS = IMPORT_FORMATS
_IMPORT_RESOLUTIONS = IMPORT_RESOLUTIONS

_SPORTS = [
    "running",
    "strength",
    "mobility",
    "recovery",
    "rest",
    "cycling",
    "swimming",
    "hiking",
    "rowing",
]
_BODY_STRESS = ["lower", "upper", "full", "systemic"]
_COSTS = ["easy", "moderate", "hard"]
_PRIORITIES = ["anchor", "flexible", "optional"]

_SESSION_CHANGE: dict[str, Any] = {
    "type": "object",
    "description": (
        "One operation on one session; each session may appear at most once. keep, "
        "move, reduce and replace name an existing session_id; add creates a session "
        "and the gateway names it. There is no remove: to drop a session, replace it "
        "with a rest or recovery session so the decision stays visible in the plan's "
        "history. plan is required for replace and add, and required on a reduce whose "
        "session is laid out along time -- shortening such a session without saying "
        "what it now holds would leave what the watch executes describing the session "
        "it used to be. On any other reduce plan is optional and the stored one stands."
    ),
    "required": ["operation"],
    "properties": {
        "operation": {
            "type": "string",
            "enum": ["keep", "move", "reduce", "replace", "add"],
        },
        "session_id": {
            "type": "string",
            "description": (
                "Required for keep, move, reduce and replace. Never sent for add."
            ),
        },
        "scheduled_date": {
            "type": "string",
            "description": "ISO date. Required for move and add; optional for replace.",
        },
        "planned_minutes": {
            "type": "integer",
            "description": (
                "Required for reduce (must be below the current value), replace and add."
            ),
        },
        "plan": _SESSION_PLAN,
        "purpose": {
            "type": "string",
            "description": (
                "Why this session exists, and the title a strength day reaches the "
                "athlete's watch under. Required for replace and add; optional for "
                "reduce and keep. On keep this is the only field you may send. Intent "
                "only, never a prescription -- a number wearing a unit (4:30/km, 5km, "
                "80kg, 150bpm, 85%) is refused and the error names it."
            ),
        },
        "coach_note": {
            "type": ["string", "null"],
            "description": (
                "One sentence you want beside this session on the day -- why this week's "
                "long run is deliberately short, when to stop a set. Optional on every "
                "operation, including keep, which is how you say something about a session "
                "without changing it. It travels to the athlete's calendar at the end of "
                "the entry's description, so changing it re-delivers the workout. Words, "
                "not a specification: a number wearing a unit (4:30/km, 5km, 80kg, 150bpm, "
                "85%) is refused and the error names it -- put the number in plan. Null "
                "removes the note the session is carrying."
            ),
        },
        "measures": {
            "type": ["string", "null"],
            "description": (
                "On replace and add only. The session_id this session repeats for the "
                "cycle's measurement, matching goal.measurement.reference_session_id -- "
                "send it when you schedule the comparison in the measurement week. It "
                "makes an ordinary session the measurement point; there is no "
                "measurement session type, and nothing about delivery changes. Null on "
                "a replace clears it."
            ),
        },
        "sport": {
            "type": "string",
            "enum": _SPORTS,
            "description": (
                "Required for add; for replace it defaults to the session's current sport."
            ),
        },
        "adaptation": {
            "type": "string",
            "enum": _ADAPTATIONS,
            "description": "Required for replace and add.",
        },
        "cost": {
            "type": "string",
            "enum": _COSTS,
            "description": "Required for replace and add.",
        },
        "body_stress": {
            "type": "string",
            "enum": _BODY_STRESS,
            "description": "Required for add; kept as-is for replace unless given.",
        },
        "priority": {
            "type": "string",
            "enum": _PRIORITIES,
            "description": "Required for add; kept as-is for replace unless given.",
        },
        "time_window": {
            "type": ["string", "null"],
            "description": "Optional, for example morning or evening.",
        },
        "fallback": {**_FALLBACK, "description": (
            "What to do when the session cannot be done as written. Required for add."
        )},
    },
}

# The apply side of a prepare/apply pair deliberately does not restate the request
# schema. The contract there is not "an object of this shape" but "the identical object
# you already sent to prepare": the proposal cryptographically binds that exact content,
# and a re-authored request -- however schema-valid -- is refused as a mismatch. Inlining
# the full shape a second time would double the size every conversation pays for the
# catalogue and invite the model to rebuild what it must resend. The prepare tool holds
# the authoritative shape.
_RESEND_CHANGE_REQUEST: dict[str, Any] = {
    "type": "object",
    "description": (
        "The identical change_request you sent to prepareCoachDecision, resent "
        "unchanged. Do not re-author it: the proposal binds that exact content, and "
        "any difference is refused."
    ),
}


_COACH_CHANGE_REQUEST: dict[str, Any] = {
    "type": "object",
    "description": (
        "One small coaching change, carrying coaching judgment only. The gateway "
        "projects it onto the current PlanState: it copies every field you did not "
        "change, and it owns the resulting PlanState and DecisionEvent, their versions, "
        "ids, hashes, timestamps, and delivery bookkeeping. Never send a PlanState or a "
        "DecisionEvent.\n\n"
        "This is also how an account's first plan is written, so which fields are "
        "required depends on which of the two you are sending, and the gateway says so "
        "by name when one is missing. A change needs summary, reason_codes, evidence, "
        "goal_effect and next_review_condition. A first plan -- after "
        "no_plan_state, with no plan_id -- needs summary, evidence, goal, cycle, "
        "week.intent and sessions, every session carrying operation \"add\"; it may "
        "not carry reason_codes, goal_effect or next_review_condition, because there is "
        "no earlier plan for them to describe."
    ),
    "properties": {
        "availability": {
            "type": "object",
            "description": (
                "First plan only. When the athlete can train and with what. Echoed in "
                "the preview for them to correct; the sessions are where it takes "
                "effect. Afterwards this is recordAthleteAvailability, not a change."
            ),
            "properties": {
                "days": {"type": "array", "items": {"type": "string"}},
                "equipment": {"type": "array", "items": {"type": "string"}},
            },
        },
        "summary": {
            "type": "string",
            "description": (
                "One line saying what changes and why, in the athlete's own language."
            ),
        },
        "reason_codes": {
            "type": "array",
            "minItems": 1,
            "description": "Why this change is being made; at least one, no repeats.",
            "items": {
                "type": "string",
                "enum": [
                    "actual_load_above_plan",
                    "actual_load_below_plan",
                    "multi_signal_recovery_down",
                    "recovery_signal_mixed",
                    "lower_body_stress_conflict",
                    "quality_session_conflict",
                    "schedule_or_equipment_changed",
                    "goal_priority_changed",
                    "data_stale_or_missing",
                    "pain_or_illness_flag",
                    "plan_kept_no_material_change",
                ],
            },
        },
        "evidence": {
            "type": "array",
            "minItems": 1,
            "description": (
                "What you actually read, and where. Never cite evidence the session "
                "response did not contain."
            ),
            "items": {
                "type": "object",
                "required": ["field", "observation"],
                "properties": {
                    "field": {
                        "type": "string",
                        "description": (
                            "Where you read it, for example recovery_trends.hrv or "
                            "recent_actuals."
                        ),
                    },
                    "observation": {"type": "string", "description": "What it said."},
                },
            },
        },
        "goal_effect": {
            "type": "object",
            "required": ["week", "cycle"],
            "properties": {
                "week": {
                    "type": "string",
                    "description": "What this does to the current week.",
                },
                "cycle": {
                    "type": "string",
                    "description": "What this does to the 28-day direction.",
                },
            },
        },
        "next_review_condition": {
            "type": "string",
            "description": "What should trigger the next look at this plan.",
        },
        "unknowns": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "What you could not establish. The gateway adds the context's own "
                "unknowns to these."
            ),
        },
        "sessions": {
            "type": "array",
            "description": (
                "The session changes. Omit or leave empty when only the goal, cycle or "
                "week text changes."
            ),
            "items": _SESSION_CHANGE,
        },
        "goal": {
            "type": "object",
            "description": (
                "Send only when the 28-day outcome itself changes; the two prose fields "
                "are then required. It replaces the goal whole, so a measurement that "
                "still holds has to be restated with it."
            ),
            "required": ["outcome", "measurement_protocol"],
            "properties": {
                "outcome": {"type": "string"},
                "measurement_protocol": {"type": "string"},
                "measurement": _MEASUREMENT,
            },
        },
        "cycle": {
            "type": "object",
            "description": "Send only the cycle fields that change; everything else is kept.",
            "properties": {
                "start": {"type": "string"},
                "end": {"type": "string"},
                "primary_adaptation": {"type": "string", "enum": _ADAPTATIONS},
                "maintenance_adaptation": {
                    "type": ["string", "null"],
                    "enum": [*_ADAPTATIONS, None],
                },
                "planned_evidence": {"type": "array", "items": {"type": "string"}},
                "adjust_conditions": {"type": "array", "items": {"type": "string"}},
                "stop_conditions": {"type": "array", "items": {"type": "string"}},
                "outlook": {
                    "type": "array",
                    "maxItems": 3,
                    "items": _OUTLOOK_WEEK,
                    "description": (
                        "The weeks of this cycle after the new current week, replacing "
                        "the previous outlook whole. Send it whenever the week rolls or "
                        "the direction moves; the week just made precise leaves it."
                    ),
                },
            },
        },
        "athlete_baseline": {
            "type": "object",
            "description": (
                "Send only the baseline fields the evidence has moved; everything else "
                "is kept. Values are measured figures, never null -- clearing an anchor "
                "is not available here. strength_loads updates or adds the movements it "
                "names and touches no other movement; naming load_kg or assist_kg "
                "replaces that pair, scheme and display_name are kept unless restated."
            ),
            "properties": {
                "threshold_pace_sec_per_km": {"type": "integer"},
                "max_hr": {"type": "integer"},
                "easy_hr_ceiling": {
                    "type": "integer",
                    "description": (
                        "Anchors hr_ceiling workout targets alongside any measured "
                        "max_hr; the higher of whichever bounds are stated is the "
                        "one used."
                    ),
                },
                "max_session_minutes": {"type": "integer"},
                "longest_recent_run_km": {"type": "number"},
                "weekly_volume_km_4wk_avg": {"type": "number"},
                "strength_loads": {
                    "type": "array",
                    "minItems": 1,
                    "items": {
                        "type": "object",
                        "required": ["exercise"],
                        "properties": {
                            "exercise": {"type": "string"},
                            "load_kg": {"type": "number"},
                            "assist_kg": {"type": "number"},
                            "scheme": {"type": ["string", "null"]},
                            "display_name": {"type": "string"},
                        },
                    },
                },
            },
        },
        "week": {
            "type": "object",
            "description": "Send only the week fields that change.",
            "properties": {
                "start": {"type": "string"},
                "intent": {"type": "string"},
            },
        },
    },
}

_TIMEZONE_PROPERTY: dict[str, Any] = {
    "type": "string",
    "description": (
        "Overrides the athlete's stored timezone for this call only, e.g. while they "
        "are travelling. Omit it otherwise: recordAthleteProfile is what sets the one "
        "every call uses."
    ),
}

# One vocabulary of symptoms, spelled the same wherever the athlete reports one. The
# descriptions differ per tool because what an unstated symptom means differs; the five
# names never do.
_RED_FLAG_PROPERTIES: dict[str, Any] = {
    "pain": {"type": ["boolean", "null"]},
    "illness": {"type": ["boolean", "null"]},
    "chest_pain": {"type": ["boolean", "null"]},
    "dizziness": {"type": ["boolean", "null"]},
    "unusual_symptoms": {"type": ["boolean", "null"]},
}

_RECOVERY_SIGNALS_DAY_UPLOAD: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    # Only the day itself is required. An athlete reading one number off a watch face has
    # exactly one number, and making them write out an explicit null for every reading
    # they do not have contradicts what omission already means on every other field of
    # this surface (issue #187). The gateway fills each unsent observation with null on
    # the way in, so the stored CoachContext still carries every key it has always carried.
    "required": ["date"],
    "properties": {
        "date": {"type": "string", "format": "date"},
        "readiness_score": {
            "type": ["number", "null"],
            "minimum": 0,
            "maximum": 100,
        },
        "readiness_level": {"type": ["string", "null"], "minLength": 1},
        "hrv_status": {"type": ["string", "null"], "minLength": 1},
        "hrv_7d_avg_ms": {"type": ["number", "null"], "exclusiveMinimum": 0},
        "acute_load": {"type": ["number", "null"], "minimum": 0},
        "recovery_time_sec": {"type": ["number", "null"], "minimum": 0},
        "body_battery_high": {
            "type": ["number", "null"],
            "minimum": 0,
            "maximum": 100,
        },
        "body_battery_low": {
            "type": ["number", "null"],
            "minimum": 0,
            "maximum": 100,
        },
        "avg_stress": {
            "type": ["number", "null"],
            "minimum": 0,
            "maximum": 100,
        },
        # The five below carry a description where the nine above do not, because these
        # are the names a client can read the wrong way: two sleep scores that mean
        # different spans, and an HRV field beside an existing seven-day average.
        "sleep_score": {
            "type": ["number", "null"],
            "minimum": 0,
            "maximum": 100,
            "description": "Last night's sleep score, 0-100, as the device stated it.",
        },
        "sleep_duration_sec": {
            "type": ["number", "null"],
            "minimum": 0,
            "maximum": 86400,
            "description": "Total sleep last night, in seconds.",
        },
        "sleep_history_score": {
            "type": ["number", "null"],
            "minimum": 0,
            "maximum": 100,
            "description": (
                "Recent sleep trend across several nights, 0-100 -- not last night's "
                "own score."
            ),
        },
        "hrv_last_night_ms": {
            "type": ["number", "null"],
            "exclusiveMinimum": 0,
            "description": (
                "Last night's average HRV in milliseconds -- the single night beside "
                "hrv_7d_avg_ms's seven-day average."
            ),
        },
        "resting_hr_bpm": {
            "type": ["number", "null"],
            "minimum": 20,
            "maximum": 150,
            "description": "Resting heart rate in beats per minute.",
        },
    },
}

_RECOVERY_SIGNALS_UPLOAD: dict[str, Any] = {
    "type": ["object", "null"],
    "additionalProperties": False,
    "description": (
        "Optional recovery readings from the athlete's own device or app, however this "
        "client came by them -- read off the watch and dictated, pasted from an export, "
        "or read by the client itself. The route is never asked about; the values and "
        "the declared source are. A number the athlete reads out from what their device "
        "displays is an ordinary observation here. Never send a database path, "
        "credential, raw provider payload, or a figure the model invented, estimated, or "
        "translated from how the athlete says they feel. Only date is required per day, "
        "so send the readings there are. Send at most the current seven observed days; "
        "the gateway derives the exact window from this session, labels the declared "
        "source as client-uploaded, and keeps it only in this CoachContext. Omission "
        "stays unknown and never blocks ordinary coaching."
    ),
    "required": ["source", "days"],
    "properties": {
        "source": {
            "type": "string",
            "minLength": 1,
            "maxLength": 80,
            "pattern": "^[A-Za-z0-9][A-Za-z0-9_.:+-]*$",
            "description": (
                "Where these readings came from, as a short label -- e.g. "
                "athlete-reported, garmin-connect-app, csv-export, "
                "personal-os:recovery_daily. Never a path, URL, credential, or note. "
                "This is declared provenance for the coach to weigh, not a provider "
                "observation made by the hosted gateway."
            ),
        },
        "days": {
            "type": "array",
            "maxItems": 7,
            "items": _RECOVERY_SIGNALS_DAY_UPLOAD,
        },
    },
}


# --------------------------------------------------------------------------------------
# The tools
# --------------------------------------------------------------------------------------


def _hints(
    title: str,
    *,
    read_only: bool,
    destructive: bool,
    idempotent: bool,
    affects_intervals: bool,
) -> dict[str, Any]:
    """One tool's behavioural annotations, every hint stated rather than defaulted.

    A client reads these to decide what to show the athlete before it calls: a
    read-only tool can run without asking, a destructive one earns a stronger prompt,
    and an idempotent one is safe to retry after a timeout. The protocol's own defaults
    are the cautious ones (not read-only, destructive, not idempotent, open world), so
    an omitted hint is not neutral -- it is a claim. Every keyword here is required at
    the call site, ``destructive`` included: it used to carry ``False`` as its default,
    which made the protocol's *least* cautious value the one a new tool got by saying
    nothing, and that is how nine tools below came to claim they only ever add.

    ``read_only`` is about the athlete's state -- their plan, their evidence, their
    Intervals account -- and not about whether any byte on the server moved. Every call
    here increments an operator's usage counter (``CoachGateway._count_usage``), and
    reading that as a write would make all 22 tools non-read-only and the hint worthless.
    What a client is deciding with it is whether to ask the athlete first, and nothing
    they would want to be asked about happens in a counter of how often they called.
    ``idempotent`` is drawn on the same line and for the same reason: a repeat leaves the
    athlete exactly where the first call did.

    Two descriptions below still say a preview "writes nothing", which the usage counter
    made imprecise -- it changes no plan and removes nothing, but a counter row is
    written. That wording is left alone deliberately. A description is part of the tool
    catalogue, and `docs/distribution/openai-review-conformance.md` prices a catalogue
    change at a re-scan and a new plugin version **on every directory at once**. Nobody is
    misled by it in the meantime: what a caller decides with that sentence is whether the
    call is safe to make, and a counter does not change the answer. Fix it in the next
    change that is already paying the catalogue cost, not on its own.

    ``destructive`` is ``destructiveHint``, and the specification's line is narrower
    than the English word: "If true, the tool may perform destructive updates to its
    environment. If false, the tool performs only additive updates." So the question is
    not "does this delete something" but "can this leave a value the athlete had
    already stated unreachable" -- a same-day correction that overwrites the previous
    report answers yes, even though nothing was deleted and the athlete asked for it.
    ``False`` is the strong, narrow claim here; ``True`` is the protocol's own default.

    ``affects_intervals`` is ``openWorldHint``, and the two specifications that define
    it do not say the same thing. MCP 2025-06-18: "this tool may interact with an
    'open world' of external entities", with a read-only web search as its example of
    open. OpenAI's plugin guidance (developers.openai.com/plugins/build/mcp-server):
    "true when a tool can affect public or external systems". This catalogue is
    reviewed and consumed under the second definition -- the hint decides how much
    confirmation a client wraps around a call, and what deserves that friction is a
    tool that can *change* the athlete's provider account, not one that reads it. So
    the question here is: can this operation leave Intervals different than it found
    it. Reading activities, wellness, the calendar, or sport settings answers no.
    """
    return {
        "title": title,
        "readOnlyHint": read_only,
        "destructiveHint": destructive,
        "idempotentHint": idempotent,
        "openWorldHint": affects_intervals,
    }


@dataclass(frozen=True)
class Tool:
    """One MCP tool and the gateway route kind it dispatches to.

    ``name`` is what the athlete's client shows and what a transcript records, so it is
    the capability's one name everywhere: the CLI subcommand, the gateway route and this
    tool do not get to spell the same operation three ways.

    ``output_schema`` describes the payload the *model* receives -- the gateway response
    after ``redactions`` -- not the gateway response itself. It stays deliberately open
    (no ``additionalProperties: false``, minimal ``required``): a client is invited to
    validate what is listed, never to refuse a coaching field this server learns to say
    later, because a schema that froze the payload would turn every new piece of
    evidence into a catalogue change and a resubmission.

    ``redactions`` is this tool's model-facing projection: each entry is a key path
    removed from the gateway response before it reaches the model. ``"*"`` steps into
    every element of a list. Only named paths are touched -- an opaque binding such as
    ``delivery_set`` or a sealed ``proposal`` is never entered, because its bytes are
    what the approval hash covers. What a path removes is audit material the store
    keeps and the owner export still serves; the model's copy is the projection, never
    the record.
    """

    name: str
    kind: str
    description: str
    input_schema: dict[str, Any]
    annotations: dict[str, Any]
    output_schema: dict[str, Any]
    redactions: tuple[tuple[str, ...], ...] = ()

    def descriptor(self) -> dict[str, Any]:
        return {
            "name": self.name,
            # A human-readable name in both of the places the specification defines one.
            # `Tool.title` is where 2025-06-18 puts it; `annotations.title` is where it
            # was before, and clients written against either revision read only their
            # own. Two spellings of one string is cheaper than a client falling back to
            # `applyWorkoutDelivery` in front of an athlete.
            "title": self.annotations["title"],
            "description": self.description,
            "inputSchema": self.input_schema,
            "outputSchema": self.output_schema,
            "annotations": self.annotations,
        }


def _redact(payload: dict[str, Any], redactions: tuple[tuple[str, ...], ...]) -> dict[str, Any]:
    """The model-facing copy of one gateway response: named paths removed, nothing else.

    Copies only along the paths it removes, so every untouched subtree -- including the
    opaque ``delivery_set`` and ``proposal`` bindings whose exact bytes an approval hash
    covers -- is the gateway's own object, byte for byte. A path that does not exist in
    this branch's response is simply not there to remove.
    """
    if not redactions:
        return payload

    def drop(node: Any, path: tuple[str, ...]) -> Any:
        key, rest = path[0], path[1:]
        if key == "*":
            if not isinstance(node, list):
                return node
            dropped = [drop(item, rest) for item in node]
            # Same identity discipline as the dict branch below: a list none of whose
            # elements changed is the gateway's own list, so the ancestors above it are
            # not copied either.
            if all(replaced is item for replaced, item in zip(dropped, node)):
                return node
            return dropped
        if not isinstance(node, dict) or key not in node:
            return node
        if not rest:
            copied = dict(node)
            del copied[key]
            return copied
        replaced = drop(node[key], rest)
        if replaced is node[key]:
            return node
        copied = dict(node)
        copied[key] = replaced
        return copied

    projected: Any = payload
    for path in redactions:
        projected = drop(projected, path)
    return projected


def _output(
    properties: dict[str, Any], *, status: str | None = None
) -> dict[str, Any]:
    """One tool's ``outputSchema``: the projected result's stable keys, held open.

    ``required`` is ``status`` alone, deliberately: every other key is branch-dependent
    (a first session has no plan, a clean delivery has an empty ``unresolved``), and a
    ``required`` that overclaims turns a legitimate branch into a validation failure in
    any client that takes the schema at its word. Nested shapes stay ``object``/``array``
    except where a key carries the next call's argument -- those say so, because that
    sentence is the contract a model must not lose.
    """
    described_status: dict[str, Any] = {"type": "string"}
    if status is not None:
        described_status["description"] = status
    return {
        "type": "object",
        "properties": {"status": described_status, **properties},
        "required": ["status"],
    }


# The response envelope `CoachGateway.route` returns and the model does not need: an
# API version constant and a response timestamp are exactly the "internal metadata" a
# reviewed tool result is expected not to carry. The three account-lifecycle tools below
# keep theirs -- an archive's timestamp is part of the archive, not debris beside it.
_ENVELOPE_REDACTIONS: tuple[tuple[str, ...], ...] = (("api_version",), ("generated_at",))

# The evidence store's own schema tag, echoed by every conversational record route. The
# store needs it; the model has nothing to branch on it.
_EVIDENCE_VERSION_REDACTION: tuple[tuple[str, ...], ...] = (("athlete_evidence_version",),)


def _record_id_redactions(echo_key: str, *id_keys: str) -> tuple[tuple[str, ...], ...]:
    """The store-provenance keys one record route echoes, removed everywhere they echo.

    Content-hash ids appear in three places -- top level, the stored echo, and the
    displaced row -- and one rule covers them, stated once: the store dedupes on these
    hashes, retraction is keyed by the record's own facts (exercise, sport, date), so no
    id is anything the model sends back. ``recorded_at`` rides the same projection for
    the same reason: it stamps when the store wrote the row, which the store keeps and
    the export serves, while the record's own *when* -- the ``date`` the athlete stated,
    a session's ``started_at`` -- is evidence and stays.
    """
    paths: list[tuple[str, ...]] = [(echo_key, "recorded_at"), ("replaced", "recorded_at")]
    for key in id_keys:
        paths.extend(((key,), (echo_key, key), ("replaced", key)))
    return tuple(paths)


# The cold-start view (`startCoachSession` before any plan exists) hands the model whole
# stored rows and whole provider actuals, because a first conversation must not re-ask
# what the record already holds. Whole rows carry the same store bookkeeping the record
# echoes above project away -- content-hash ids and write instants -- plus the provider's
# own activity id and event pairing, which a first plan has nothing to bind to. All of it
# leaves here, by the one rule the rest of this file already follows: the athlete's own
# facts (dates, sets, sentences, sources, provider overlap flags) stay, the bookkeeping
# does not. The full rows still reach the store and the owner export unchanged.
_PRE_PLAN_EVIDENCE = ("pre_plan_observations", "athlete_evidence")
_PRE_PLAN_REDACTIONS: tuple[tuple[str, ...], ...] = tuple(
    _PRE_PLAN_EVIDENCE + tail
    for tail in (
        ("availability", "recurring", "recorded_at"),
        ("availability", "effective_this_week", "recorded_at"),
        ("strength_reports", "*", "report_id"),
        ("strength_reports", "*", "recorded_at"),
        ("body_measurements", "*", "measurement_id"),
        ("body_measurements", "*", "recorded_at"),
        ("reported_activities", "*", "summary_id"),
        ("reported_activities", "*", "dedup_keys"),
        ("reported_activities", "*", "recorded_at"),
        ("long_term_goals", "*", "recorded_at"),
        ("training_preferences", "*", "recorded_at"),
        ("subjective_states", "*", "state_id"),
        ("subjective_states", "*", "recorded_at"),
    )
) + (
    ("pre_plan_observations", "recent_training", "recent_actuals", "*", "activity_id"),
    ("pre_plan_observations", "recent_training", "recent_actuals", "*", "paired_event_id"),
)

_SESSION_OUTPUT = _output(
    {
        "plan_state": {
            "type": "object",
            "description": (
                "present, plan_id, plan_version, and the full current PlanState. "
                "plan_id and plan_version are what prepare/apply calls take."
            ),
        },
        "context": {
            "type": ["object", "null"],
            "description": (
                "The CoachContext this session judged from; null before a plan exists. "
                "Send it back verbatim on prepareCoachDecision and applyCoachDecision."
            ),
        },
        "validation": {"type": ["object", "null"]},
        "unknowns": {"type": "array"},
        "delivery": {
            "type": ["object", "null"],
            "description": (
                "Per-session delivery evidence; unresolved_delivery.attempt_id is what "
                "clearDeliveryAttempt takes."
            ),
        },
        "reconciliation": {"type": ["object", "null"]},
        "pre_plan_observations": {"type": "object"},
        "coaching_guidance": {"type": "string"},
    },
    status='"passed", or "no_plan_state" when no plan exists yet.',
)

_STATE_OUTPUT = _output(
    {
        "plan_id": {"type": ["string", "null"]},
        "plan_version": {"type": ["integer", "null"]},
        "cycle": {"type": ["object", "null"]},
        "week": {"type": ["object", "null"]},
        "goal": {"type": ["object", "null"]},
        "delivery": {"type": ["object", "null"]},
        "pending_delivery_attempt_id": {
            "type": ["string", "null"],
            "description": "The open reservation clearDeliveryAttempt takes, when one exists.",
        },
        "unknowns": {"type": "array"},
    }
)

_PERMISSIONS_OUTPUT = _output(
    {
        "scopes_recorded_at_authorization": {"type": ["array", "null"]},
        "settings_read": {"type": "string"},
        "calendar_read": {"type": "string"},
    }
)

_PROFILE_RECORD_OUTPUT = _output(
    {"profile": {"type": "object"}, "effective": {"type": "object"}}
)

_AVAILABILITY_RECORD_OUTPUT = _output(
    {
        "idempotent_replay": {"type": "boolean"},
        "recurring": {"type": ["object", "null"]},
        "week": {"type": ["object", "null"]},
        "effective_this_week": {"type": "object"},
    }
)

_LONG_TERM_GOAL_OUTPUT = _output(
    {
        "goal": {"type": "object"},
        "replaced": {"type": ["object", "null"]},
        "long_term_goals": {"type": "array"},
    }
)

_TRAINING_PREFERENCE_OUTPUT = _output(
    {
        "preference": {"type": "object"},
        "replaced": {"type": ["object", "null"]},
        "training_preferences": {"type": "array"},
    }
)

_STRENGTH_REPORT_OUTPUT = _output(
    {
        "idempotent_replay": {"type": "boolean"},
        "replaced": {"type": ["object", "null"]},
        "report": {"type": "object"},
        "report_count": {"type": "integer"},
    }
)

_BODY_MEASUREMENT_OUTPUT = _output(
    {
        "idempotent_replay": {"type": "boolean"},
        "replaced": {"type": ["object", "null"]},
        "measurement": {"type": "object"},
        "measurement_count": {"type": "integer"},
    }
)

_ACTIVITY_SUMMARY_OUTPUT = _output(
    {
        "idempotent_replay": {"type": "boolean"},
        "replaced": {"type": ["object", "null"]},
        "replaced_note": {"type": ["string", "null"]},
        "activity": {"type": "object"},
        "activity_count": {"type": "integer"},
    }
)

_SUBJECTIVE_STATE_OUTPUT = _output(
    {
        "idempotent_replay": {"type": "boolean"},
        "replaced": {"type": ["object", "null"]},
        "state": {"type": "object"},
        "state_count": {"type": "integer"},
    }
)

_HISTORY_IMPORT_OUTPUT = _output(
    {
        "format": {"type": "string"},
        "recognised_as": {"type": ["string", "null"]},
        "already_imported": {"type": "boolean"},
        "counts": {"type": "object"},
        "added": {"type": "object"},
        "merged": {"type": "object"},
        "skipped": {"type": "object"},
        "needs_confirmation": {
            "type": "object",
            "description": (
                "items[].conflict_id is what a repeat call's resolutions[].conflict_id "
                "must copy."
            ),
        },
        "measurements_added": {"type": "object"},
        "measurements_skipped": {"type": "object"},
        "unreadable": {"type": "object"},
        "note": {"type": ["string", "null"]},
    }
)

_RETRACT_OUTPUT = _output(
    {
        "retracted": {"type": "boolean"},
        "removed": {"type": ["object", "null"]},
        "record_count": {"type": "integer"},
        "on_record_that_day": {"type": ["array", "null"]},
        "candidates": {
            "type": "array",
            "description": (
                "Non-empty when one day holds several sessions of the sport: repeat the "
                "call copying the started_at of the one the athlete meant."
            ),
        },
        "note": {"type": ["string", "null"]},
    }
)

_STRENGTH_CONFIRM_OUTPUT = _output(
    {
        "plan_id": {"type": "string"},
        "plan_version": {"type": "integer"},
        "date": {"type": "string"},
        "session_id": {"type": "string"},
        "source": {"type": "string"},
        "movements": {"type": "array"},
        "report_count": {"type": "integer"},
        "idempotent_replay": {"type": "boolean"},
    }
)

_DECISION_PREPARE_OUTPUT = _output(
    {
        "plan_id": {"type": ["string", "null"]},
        "base_version": {"type": ["integer", "null"]},
        "resulting_version": {"type": ["integer", "null"]},
        "plan_version": {"type": ["integer", "null"]},
        "proposal": {
            "type": "string",
            "description": "Send back verbatim on applyCoachDecision.",
        },
        "expires_at": {"type": "string"},
        "confirmation_required": {"type": "boolean"},
        "preview": {"type": "object"},
        "validation": {"type": "object"},
        "warnings": {"type": "array"},
        "unknowns": {"type": "array"},
        "athlete_profile": {"type": ["object", "null"]},
    }
)

_DECISION_APPLY_OUTPUT = _output(
    {
        "plan_id": {"type": "string"},
        "plan_version": {"type": "integer"},
        "idempotent_replay": {"type": "boolean"},
        "validation": {"type": "object"},
        "warnings": {"type": "array"},
    }
)

_DELIVERY_PREPARE_OUTPUT = _output(
    {
        "plan_id": {"type": "string"},
        "plan_version": {"type": "integer"},
        "proposal_hash": {
            "type": "string",
            "description": (
                "Send back verbatim on applyWorkoutDelivery, beside the untouched "
                "delivery_set."
            ),
        },
        "confirmation_required": {"type": "boolean"},
        "preview": {"type": "array"},
        "settings_changes": {"type": "array"},
        "delivery_set": {
            "type": "object",
            "description": "Opaque approved set: send back byte-identical, never edited.",
        },
    }
)

_DELIVERY_APPLY_OUTPUT = _output(
    {
        "delivery_state": {"type": "string"},
        "max_delivery_state": {"type": "string"},
        "plan_id": {"type": "string"},
        "plan_version": {"type": "integer"},
        "proposal_hash": {"type": "string"},
        "settings_changes": {"type": "array"},
        "delivered": {"type": "array"},
        "withdrawn": {"type": "array"},
        "unresolved": {
            "type": "array",
            "description": (
                'Non-empty on status "partial": retry with the same delivery_set and '
                "proposal_hash to converge, never a fresh prepare."
            ),
        },
        "attempt_open": {"type": "boolean"},
        "state_update": {"type": "object"},
    },
    status='"passed", or "partial" when Intervals holds fewer sessions than approved.',
)

_ATTEMPT_CLEAR_OUTPUT = _output(
    {
        "cleared": {"type": "boolean"},
        "attempt_id": {"type": ["string", "null"]},
        "abandoned": {"type": "array"},
        "detail": {"type": "string"},
    }
)

_DATA_EXPORT_OUTPUT = _output(
    {
        "api_version": {"type": "string"},
        "generated_at": {"type": "string"},
        "archive_version": {"type": "string"},
        "owner_reference": {
            "type": "string",
            "description": "Opaque account handle, safe for the athlete to quote publicly.",
        },
        "identity": {"type": "object"},
        "plan_state": {"type": ["object", "null"]},
        "decision_history": {"type": "array"},
        "athlete_evidence": {"type": "object"},
        "unresolved_delivery": {"type": ["string", "null"]},
        "excluded": {"type": "array"},
        "unknowns": {"type": "array"},
    }
)

_DELETION_PREPARE_OUTPUT = _output(
    {
        "api_version": {"type": "string"},
        "generated_at": {"type": "string"},
        "proposal": {
            "type": "string",
            "description": "Send back verbatim on applyOwnerDeletion.",
        },
        "expires_at": {"type": "string"},
        "confirmation_required": {"type": "boolean"},
        "removes": {"type": "object"},
        "not_removed": {"type": "array"},
        "reversible": {"type": "boolean"},
    }
)

_DELETION_APPLY_OUTPUT = _output(
    {
        "api_version": {"type": "string"},
        "generated_at": {"type": "string"},
        "deleted": {"type": "boolean"},
        "receipt_id": {"type": "string"},
        "removed": {"type": "object"},
        "not_removed": {"type": "array"},
    }
)


TOOLS: tuple[Tool, ...] = (
    Tool(
        name="startCoachSession",
        kind="session",
        output_schema=_SESSION_OUTPUT,
        # `context_id` is redacted as a top-level duplicate only: the same value stays
        # inside `context`, whose bytes the decision proposal hashes. The cold-start
        # paths reach only `pre_plan_observations` -- on the with-plan branch that key
        # is absent and every one of them is a no-op, and none can touch `context` or
        # `plan_state`.
        redactions=_ENVELOPE_REDACTIONS + (("context_id",),) + _PRE_PLAN_REDACTIONS,
        # Not read-only. This is the route that applies deterministic reconciliation,
        # and reconciliation is made of store commits: a plan can come back at a higher
        # version than it went in at. A client told this were read-only would run it
        # without asking, retry it freely, and read a changed plan as its own doing.
        # Not destructive, which is the narrower claim: those commits land on an
        # append-only chain, so the version it supersedes stays readable in `commits/`.
        # Every write lands in this product's own store: Intervals is read for fresh
        # evidence and left exactly as found.
        annotations=_hints(
            "Read the plan and reconcile completed work",
            read_only=False,
            destructive=False,
            idempotent=False,
            affects_intervals=False,
        ),
        description=(
            "Call before answering any today, this-week, plan, or reassessment "
            "question; the returned PlanState is the only durable memory across "
            "conversations. The response also carries coaching_guidance -- the training "
            "judgment to coach from -- so there is nothing to fetch separately."
        ),
        input_schema={
            "type": "object",
            "description": (
                "Every field is optional. Omission means \"not confirmed this turn\", "
                "never a convenient default."
            ),
            "properties": {
                "as_of": {
                    "type": ["string", "null"],
                    "description": (
                        "ISO-8601 instant to evaluate the plan as of; defaults to the "
                        "current time."
                    ),
                },
                "timezone": {
                    "type": "string",
                    "description": (
                        "Overrides the athlete's stored timezone for this call only, "
                        "e.g. while they are travelling. Omit it otherwise: their "
                        "stored profile already decides what \"today\" means."
                    ),
                },
                "available_days": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Confirmed available weekday names for this week (e.g. mon, "
                        "tue); omit when not yet confirmed."
                    ),
                },
                "session_minutes": {
                    "type": ["integer", "null"],
                    "description": "Confirmed minutes available for today's session.",
                },
                "red_flags": {
                    "type": "object",
                    "description": (
                        "Tri-state per symptom -- true, false, or omitted/null for "
                        "unassessed. Never infer a value the athlete did not give. This is "
                        "where a symptom goes: a true one limits today deterministically, "
                        "here and nowhere else. Tiredness, poor sleep and heavy legs are "
                        "not symptoms -- they are recordSubjectiveState, which stores the "
                        "sentence and fires no rule."
                    ),
                    "properties": _RED_FLAG_PROPERTIES,
                },
                "all_clear": {
                    "type": "boolean",
                    "description": (
                        "Shorthand for \"no red flags reported\"; sets every red_flags "
                        "entry not otherwise given to false."
                    ),
                },
                "leg_fatigue": {
                    "type": "string",
                    "enum": ["normal", "elevated", "severe", "unknown"],
                    "description": "Defaults to unknown.",
                },
                "soreness": {
                    "type": "string",
                    "enum": ["normal", "elevated", "severe", "unknown"],
                    "description": "Defaults to unknown.",
                },
                "schedule_changed": {"type": ["boolean", "null"]},
                "equipment_changed": {"type": ["boolean", "null"]},
                "unknowns": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Additional athlete-reported unknowns to carry into the context."
                    ),
                },
                "recovery_signals": _RECOVERY_SIGNALS_UPLOAD,
            },
        },
    ),
    Tool(
        name="getCoachState",
        kind="state",
        output_schema=_STATE_OUTPUT,
        redactions=_ENVELOPE_REDACTIONS,
        # Genuinely read-only, unlike startCoachSession: no provider request is built and
        # apply_reconciliation is never called, so the store cannot change underneath it.
        # This route never contacts Intervals at all -- affects_intervals is false here
        # for that reason, where the session and permission reads earn the same value by
        # reading the provider and leaving it unchanged.
        annotations=_hints(
            "Read the stored plan summary",
            read_only=True,
            destructive=False,
            idempotent=True,
            affects_intervals=False,
        ),
        description=(
            "Call for a plain status check: current plan id, version, week and delivery "
            "summary -- zero writes, zero Intervals calls, and it cannot reconcile. Use "
            "startCoachSession instead whenever the answer needs fresh evidence."
        ),
        # Takes nothing: the bearer alone decides whose store this reads.
        input_schema={"type": "object", "properties": {}},
    ),
    Tool(
        name="inspectIntervalsPermissions",
        kind="permissions",
        output_schema=_PERMISSIONS_OUTPUT,
        redactions=_ENVELOPE_REDACTIONS,
        # Asks the provider what this credential can do, and changes nothing on either
        # side -- a probe, not an effect.
        annotations=_hints(
            "Check the Intervals connection",
            read_only=True,
            destructive=False,
            idempotent=True,
            affects_intervals=False,
        ),
        description=(
            "Call only when debugging a connection. Reads Settings and the calendar "
            "once each and classifies what the provider allowed now; the recorded scope "
            "list is only what the token said when it was issued. Never returns provider "
            "settings, calendar contents, or credentials."
        ),
        # Takes nothing: the connected token is the whole input, and it never travels in
        # a tool argument.
        input_schema={"type": "object", "properties": {}},
    ),
    Tool(
        name="recordAthleteProfile",
        kind="profile_record",
        output_schema=_PROFILE_RECORD_OUTPUT,
        redactions=_ENVELOPE_REDACTIONS
        + _EVIDENCE_VERSION_REDACTION
        + _record_id_redactions("profile"),
        # Destructive, because each field is latest-wins: a second timezone overwrites
        # the first and the first is not kept anywhere. `athlete-evidence.json` sits
        # outside the append-only commit chain on purpose, so unlike a plan version
        # there is no earlier copy to read back.
        annotations=_hints(
            "Record where the athlete is and which language they read",
            read_only=False,
            destructive=True,
            idempotent=True,
            affects_intervals=False,
        ),
        description=(
            "Call when the athlete says where they are or which language they want "
            "their plan in. One step, and does not modify PlanState. Send only what "
            "they stated; the other field stays as it was. Restating a field replaces "
            "it. Stored once and used by every later call."
        ),
        input_schema={
            "type": "object",
            "description": (
                "At least one of timezone and language is required. Send only what the "
                "athlete stated; the other one keeps whatever it held."
            ),
            "properties": {
                "timezone": {
                    "type": "string",
                    "description": (
                        "The IANA timezone the athlete lives in, e.g. Europe/Berlin. "
                        "Decides what today and the next session mean everywhere else."
                    ),
                },
                "language": {
                    "type": "string",
                    "enum": ["zh-Hant", "en"],
                    "description": (
                        "The language their prescriptions are written in, which reaches "
                        "their watch. Movement names stay in the athlete's own words "
                        "either way."
                    ),
                },
            },
        },
    ),
    Tool(
        name="recordAthleteAvailability",
        output_schema=_AVAILABILITY_RECORD_OUTPUT,
        redactions=_ENVELOPE_REDACTIONS
        + _EVIDENCE_VERSION_REDACTION
        + (
            ("recurring", "recorded_at"),
            ("week", "recorded_at"),
            ("effective_this_week", "recorded_at"),
        ),
        kind="availability_record",
        # Destructive on the `recurring` half, which the input schema already says out
        # loud: "Sending it again replaces the previous one." The standing week is a
        # single latest-wins value, so an athlete who moves their training days leaves
        # no readable trace of the week they moved off.
        #
        # Idempotent, and its two halves reach that separately. `recurring` compares the
        # days it was sent against the ones on record and leaves the record standing when
        # they match, rather than re-stamping it with a fresh `recorded_at`. `week` is
        # append-only, so it compares against the statement standing for that week and
        # answers from it instead of layering an identical repeat -- which is what would
        # otherwise send the note to the coach twice as `week_constraints: ["出差",
        # "出差"]`. Both use the same content-versus-provenance split every other record
        # tool uses: `_same_statement` in athlete_evidence.py.
        annotations=_hints(
            "Record which days the athlete can train",
            read_only=False,
            destructive=True,
            idempotent=True,
            affects_intervals=False,
        ),
        description=(
            "Call when the athlete states available or unavailable days. One step, and "
            "does not modify PlanState. Send only the stated week or recurring days; "
            "omitted days stay unchanged. Safe to retry: re-sending a statement already "
            "on record changes nothing. A changed recurring week replaces the previous "
            "one."
        ),
        input_schema={
            "type": "object",
            "description": (
                "At least one of recurring and week is required. Weekday names are mon, "
                "tue, wed, thu, fri, sat, sun."
            ),
            "properties": {
                "timezone": _TIMEZONE_PROPERTY,
                "recurring": {
                    "type": "object",
                    "description": (
                        "The athlete's normal week, which stands until they say "
                        "otherwise. Sending it again replaces the previous one."
                    ),
                    "properties": {
                        "available_days": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Weekdays the athlete can train.",
                        },
                        "unavailable_days": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": (
                                "Weekdays the athlete cannot train. Never fill this in "
                                "as the complement of available_days; send only days "
                                "the athlete actually named."
                            ),
                        },
                    },
                },
                "week": {
                    "type": "object",
                    "description": (
                        "One week, layered onto the athlete's normal week rather than "
                        "replacing it. A day lost or gained goes in "
                        "available_days/unavailable_days and the rest of the standing "
                        "week is untouched; \"this week I can only do X and Y\" is "
                        "only_days instead, the week restated in full."
                    ),
                    "properties": {
                        "week_start": {
                            "type": "string",
                            "description": (
                                "Optional, and rarely needed. Any ISO date inside the "
                                "target week; defaults to the current week."
                            ),
                        },
                        "available_days": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": (
                                "Weekdays gained this week, on top of the athlete's "
                                "normal week."
                            ),
                        },
                        "unavailable_days": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": (
                                "Weekdays lost this week, out of the athlete's normal week."
                            ),
                        },
                        "only_days": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": (
                                "This week in full, replacing the normal week for this "
                                "week only. Mutually exclusive with available_days and "
                                "unavailable_days."
                            ),
                        },
                        "note": {
                            "type": "string",
                            "description": (
                                "One thing about this week that is not a weekday: "
                                "travelling, a hotel gym, a week that will run late. Send "
                                "it alone when no day is lost. It applies to this week "
                                "only and expires with it -- a habit that keeps standing "
                                "is recordTrainingPreference instead."
                            ),
                        },
                    },
                },
            },
        },
    ),
    Tool(
        name="recordLongTermGoal",
        output_schema=_LONG_TERM_GOAL_OUTPUT,
        redactions=_ENVELOPE_REDACTIONS
        + _EVIDENCE_VERSION_REDACTION
        + _record_id_redactions("goal")
        + (("long_term_goals", "*", "recorded_at"),),
        kind="long_term_goal_record",
        # Destructive: `_upsert_standing` keys on the metric, so restating 體重 replaces
        # the target on record for it -- the input schema below says so -- and the
        # target it displaced is gone. A goal is the longest-lived thing an athlete
        # states here, which makes overwriting one worth a client's prompt.
        annotations=_hints(
            "Record what the athlete is training for beyond this cycle",
            read_only=False,
            destructive=True,
            idempotent=True,
            affects_intervals=False,
        ),
        description=(
            "Call when the athlete states something they are training for past the "
            "current cycle -- a body weight, a VO2max, a race time, a lift. One step, "
            "and does not modify PlanState; it outlives every cycle and is read by all "
            "of them. Restating a metric replaces the goal on record for it. Never call "
            "it to record a current measurement, and never to change a goal the athlete "
            "did not change."
        ),
        input_schema={
            "type": "object",
            "required": ["metric", "target"],
            "properties": {
                "metric": {
                    "type": "string",
                    "description": (
                        "What they are aiming at, in their own words: 體重, VO2max, 5K. "
                        "Restating the same metric replaces the goal on record for it."
                    ),
                },
                "target": {
                    "type": "string",
                    "description": (
                        "Where they want it, exactly as they said it: \"50\", \"80 kg\", "
                        "\"sub-25:00\". Never a current value, and never converted."
                    ),
                },
                "target_date": {
                    "type": "string",
                    "description": "Optional ISO date they named. Omit when they named none.",
                },
                "note": {
                    "type": "string",
                    "description": "Optional; anything else they said about this goal.",
                },
            },
        },
    ),
    Tool(
        name="recordTrainingPreference",
        kind="training_preference_record",
        output_schema=_TRAINING_PREFERENCE_OUTPUT,
        redactions=_ENVELOPE_REDACTIONS
        + _EVIDENCE_VERSION_REDACTION
        + _record_id_redactions("preference")
        + (("training_preferences", "*", "recorded_at"),),
        # Destructive for the same reason as the goal above: same `_upsert_standing`,
        # keyed on the topic, so restating 長跑日 replaces the habit on record for it.
        annotations=_hints(
            "Record a training habit the athlete states",
            read_only=False,
            destructive=True,
            idempotent=True,
            affects_intervals=False,
        ),
        description=(
            "Call when the athlete states how they like to train -- 習慣週五品質跑, five "
            "strength sessions a week, chest-back-legs. One step, and does not modify "
            "PlanState. Restating a topic replaces the habit on record for it. Only "
            "what they stated: never store a pattern you read "
            "out of their history, and never rewrite one because recent training "
            "diverged from it. A preference is a starting point you may plan against "
            "with a reason, not a rule. A constraint that lasts one week is the week note "
            "on recordAthleteAvailability instead."
        ),
        input_schema={
            "type": "object",
            "required": ["topic", "statement"],
            "properties": {
                "topic": {
                    "type": "string",
                    "description": (
                        "What the habit is about, e.g. 長跑日 or 重訓頻率. Restating the "
                        "same topic replaces the habit on record for it."
                    ),
                },
                "statement": {
                    "type": "string",
                    "description": "The habit in the athlete's own words.",
                },
            },
        },
    ),
    Tool(
        name="recordStrengthExecution",
        kind="strength_report",
        output_schema=_STRENGTH_REPORT_OUTPUT,
        redactions=_ENVELOPE_REDACTIONS
        + _EVIDENCE_VERSION_REDACTION
        + _record_id_redactions("report", "report_id"),
        # Destructive, and the description already tells the caller why: correcting is
        # done by re-sending the same movement and day. `_upsert_strength_reports` holds
        # one report per (date, exercise), and the record a correction displaces "is
        # returned, never kept" -- so 70 kg sent over 65 kg loses the 65.
        annotations=_hints(
            "Record what the athlete lifted",
            read_only=False,
            destructive=True,
            idempotent=True,
            affects_intervals=False,
        ),
        description=(
            "Call when the athlete reports completed strength sets. One step, and does "
            "not modify PlanState. Send only stated values; never infer missing load or "
            "reps. Re-send the same movement/day to correct it, which replaces the "
            "earlier report for that movement; identical reports are idempotent."
        ),
        input_schema={
            "type": "object",
            "required": ["exercise", "sets"],
            "properties": {
                "timezone": _TIMEZONE_PROPERTY,
                "date": {
                    "type": "string",
                    "description": (
                        "The day the athlete performed this, as an ISO date. Optional; "
                        "defaults to today in the athlete's own timezone. May not be in "
                        "their future."
                    ),
                },
                "exercise": {
                    "type": "string",
                    "description": "The movement as the athlete named it, e.g. bench press.",
                },
                "category": {
                    "type": "string",
                    "description": (
                        "Optional metadata. Send only when the athlete volunteered it or "
                        "the plan already names it; never ask the athlete which body "
                        "part a movement trains."
                    ),
                },
                "sets": {
                    "type": "array",
                    "minItems": 1,
                    "description": (
                        "One entry per set, exactly as reported. Omit a measurement the "
                        "athlete did not give rather than estimating it."
                    ),
                    "items": {
                        "type": "object",
                        "properties": {
                            "set": {
                                "type": "integer",
                                "minimum": 1,
                                "description": (
                                    "Position of this set within the exercise. Optional; "
                                    "defaults to this set's position in the list."
                                ),
                            },
                            "weight_kg": {"type": ["number", "null"]},
                            "assist_kg": {
                                "type": ["number", "null"],
                                "description": (
                                    "Assistance load, for assisted pull-ups and similar."
                                ),
                            },
                            "reps": {"type": ["integer", "null"]},
                            "rpe": {"type": ["number", "null"]},
                        },
                    },
                },
                "notes": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "What the athlete said about the session, in their words, e.g. "
                        "that the last set was cut short."
                    ),
                },
            },
        },
    ),
    Tool(
        name="recordBodyMeasurement",
        kind="body_measurement_record",
        output_schema=_BODY_MEASUREMENT_OUTPUT,
        redactions=_ENVELOPE_REDACTIONS
        + _EVIDENCE_VERSION_REDACTION
        + _record_id_redactions("measurement", "measurement_id"),
        # Destructive: one record per day by construction, so a restatement overwrites
        # the figure already stored for that day rather than sitting beside it. The echo
        # this description promises is what makes that safe in practice, but the client
        # still deserves to know a stored number can be displaced.
        annotations=_hints(
            "Record what the athlete weighed",
            read_only=False,
            destructive=True,
            idempotent=True,
            affects_intervals=False,
        ),
        description=(
            "Call when the athlete states a weight or body fat reading. The record is "
            "written immediately and echoed back -- that echo is their chance to correct "
            "it by restating, and no PlanState changes. One record per day: re-sending "
            "replaces that day's figure, and a figure you do not send keeps whatever it "
            "held."
        ),
        input_schema={
            "type": "object",
            "description": (
                "At least one of weight_kg and body_fat_pct is required. Send only what "
                "the athlete stated; never convert, estimate, or carry a figure over "
                "from another day."
            ),
            "properties": {
                "timezone": _TIMEZONE_PROPERTY,
                "date": {
                    "type": "string",
                    "description": (
                        "The day this was measured, as an ISO date. Optional; defaults to "
                        "today in the athlete's own timezone. May not be in their future."
                    ),
                },
                "weight_kg": {
                    "type": ["number", "null"],
                    "description": (
                        "Body weight in kilograms, as stated. A figure outside 20-400 is "
                        "refused rather than stored."
                    ),
                },
                "body_fat_pct": {
                    "type": ["number", "null"],
                    "description": (
                        "Body fat as a percentage, as stated. A figure outside 1-75 is "
                        "refused rather than stored."
                    ),
                },
            },
        },
    ),
    Tool(
        name="recordActivitySummary",
        kind="activity_summary_record",
        output_schema=_ACTIVITY_SUMMARY_OUTPUT,
        redactions=_ENVELOPE_REDACTIONS
        + _EVIDENCE_VERSION_REDACTION
        + _record_id_redactions("activity", "summary_id", "dedup_keys"),
        # Destructive: one summary per sport per day, so the second swim of a day sent
        # on its own replaces the first rather than joining it -- which is exactly why
        # the description tells the caller to combine them into one summary instead.
        annotations=_hints(
            "Record a session no device recorded",
            read_only=False,
            destructive=True,
            idempotent=True,
            affects_intervals=False,
        ),
        description=(
            "Call when the athlete reports training that no watch or provider captured -- "
            "a pool session, a hike, a treadmill without the watch. The record is written "
            "immediately and echoed back for them to correct by restating; no PlanState "
            "change, and nothing is written to Intervals. It "
            "reaches the coach as athlete-reported evidence and never completes a planned "
            "session. One summary per sport per day: re-sending that sport and day "
            "replaces it, so two genuinely separate sessions of one sport go in as a "
            "single combined summary."
        ),
        input_schema={
            "type": "object",
            "required": ["sport", "duration_minutes"],
            "properties": {
                "timezone": _TIMEZONE_PROPERTY,
                "date": {
                    "type": "string",
                    "description": (
                        "The day the athlete trained, as an ISO date. Optional; defaults "
                        "to today in the athlete's own timezone. May not be in their "
                        "future."
                    ),
                },
                "sport": {
                    "type": "string",
                    "enum": [sport for sport in _SPORTS if sport != "rest"],
                    "description": "What they trained. Rest is not a session to report.",
                },
                "duration_minutes": {
                    "type": "integer",
                    "minimum": 1,
                    "description": "How long it ran, in whole minutes, as they stated it.",
                },
                "distance_km": {
                    "type": ["number", "null"],
                    "description": (
                        "Distance covered, when the athlete gave one. Never derive it "
                        "from duration."
                    ),
                },
                "subjective_feel": {
                    "type": ["integer", "null"],
                    "minimum": 1,
                    "maximum": 5,
                    "description": (
                        "How it felt, 1 (worst) to 5 (best) -- the same scale recorded "
                        "activities carry. Send only when the athlete rated it."
                    ),
                },
                "note": {
                    "type": ["string", "null"],
                    "description": "Anything else they said about it, in their own words.",
                },
            },
        },
    ),
    Tool(
        name="recordSubjectiveState",
        kind="subjective_state_record",
        output_schema=_SUBJECTIVE_STATE_OUTPUT,
        redactions=_ENVELOPE_REDACTIONS
        + _EVIDENCE_VERSION_REDACTION
        + _record_id_redactions("state", "state_id"),
        # Destructive, and this one is the easiest to get wrong: since #190 it is one
        # note per day, so a second sentence about the same day displaces the first
        # instead of adding to it. The whole point of the tool is that a run of these is
        # legible over weeks, which is precisely what a silently overwritten day breaks.
        annotations=_hints(
            "Record how the athlete says they feel",
            read_only=False,
            destructive=True,
            idempotent=True,
            affects_intervals=False,
        ),
        description=(
            "Call when the athlete says how they are feeling -- 我覺得很累, 最近睡不好, "
            "腿還是很沉. Stores their sentence and the day it is about, and nothing else: "
            "it is not translated, not scored, not turned into a recovery number, and no "
            "rule fires on it. One note per day, so restating replaces that day's note, "
            "and no PlanState changes. It "
            "exists so a run of them is visible at all -- three weeks of 很累 was three "
            "separate conversations and never a pattern -- and what a run means is yours "
            "to read from the notes and their dates, not something stored here. "
            "Symptoms do not come here: pain, illness, chest pain, dizziness or unusual "
            "symptoms are startCoachSession's red_flags, which limit the day rather than "
            "wait to be read. Only what the athlete stated, never how you read it."
        ),
        input_schema={
            "type": "object",
            "required": ["note"],
            "properties": {
                "timezone": _TIMEZONE_PROPERTY,
                "date": {
                    "type": "string",
                    "description": (
                        "The day they are describing, as an ISO date. Optional; defaults "
                        "to today in the athlete's own timezone. Send it when they are "
                        "talking about another day -- 昨天很累 is about yesterday. May not "
                        "be in their future."
                    ),
                },
                "note": {
                    "type": "string",
                    "maxLength": 600,
                    "description": (
                        "What they said, in their own words. Their sentence, not a summary "
                        "of it and not a rating derived from it. One note per day: "
                        "re-sending corrects the day, so a second thing said the same day "
                        "goes in as one sentence holding both."
                    ),
                },
            },
        },
    ),
    Tool(
        name="importAthleteHistory",
        output_schema=_HISTORY_IMPORT_OUTPUT,
        # needs_confirmation[].conflict_id stays: it is the argument a repeat call's
        # resolutions[] copies back.
        redactions=_ENVELOPE_REDACTIONS + _EVIDENCE_VERSION_REDACTION + (("import_id",),),
        kind="history_import",
        # Additive, so not destructive: it adds sessions, and where one is already on
        # record it leaves that record exactly as it stands and only writes down that the
        # upload described it too. Idempotent because the payload's own digest is what
        # recognises a re-send -- the same file dropped in twice writes nothing the second
        # time, which is the case this has to survive rather than the exception.
        annotations=_hints(
            "Import training history from a file the athlete uploaded",
            read_only=False,
            destructive=False,
            idempotent=True,
            affects_intervals=False,
        ),
        description=(
            "Call when the athlete uploads training history -- a CSV export from Strava, "
            "Intervals.icu or Garmin Connect, an Apple Health export, or a .fit file. "
            "Send the file's own text as content (base64 for .fit) rather than "
            "transcribing its rows: what this stores then came from the file. Use format "
            "records only for a source that cannot be sent as text, such as a screenshot "
            "or a PDF you read yourself. Sessions already on record are skipped or "
            "merged silently; anything genuinely ambiguous comes back in "
            "needs_confirmation, and answering is re-sending the identical payload with "
            "resolutions. Imported sessions are the athlete's own evidence, never a "
            "provider actual: they complete no planned session and are never delivered."
        ),
        input_schema={
            "type": "object",
            "required": ["format"],
            "properties": {
                "timezone": _TIMEZONE_PROPERTY,
                "format": {
                    "type": "string",
                    "enum": list(_IMPORT_FORMATS),
                    "description": (
                        "What content holds: csv for a spreadsheet export, "
                        "apple_health_xml for Apple's export.xml or any fragment of it, "
                        "fit for one base64-encoded .fit file, records when you are "
                        "supplying rows yourself."
                    ),
                },
                "content": {
                    "type": "string",
                    "description": (
                        "The file's own text, verbatim, for every format except records. "
                        "One request holds about 1 MB, but your own context is the "
                        "tighter limit -- the text has to pass through it. Send a long "
                        "history in parts, by year; each part is deduped against what "
                        "the earlier ones wrote, so parts may overlap. An Apple Health "
                        "export is far past any of that whole: pass only "
                        "the <Workout> and body-mass <Record> elements, which read "
                        "identically to the file."
                    ),
                },
                "records": {
                    "type": "array",
                    "description": (
                        "Sessions you read out of a source that cannot be sent as text. "
                        "State only what the source actually says; never fill a distance "
                        "or a duration you inferred."
                    ),
                    "items": {
                        "type": "object",
                        "required": ["date", "sport", "duration_minutes"],
                        "properties": {
                            "date": {"type": "string"},
                            "sport": {"type": "string"},
                            "duration_minutes": {"type": "integer", "minimum": 1},
                            "distance_km": {"type": ["number", "null"]},
                            "started_at": {"type": ["string", "null"]},
                            "external_id": {"type": ["string", "null"]},
                            "note": {"type": ["string", "null"]},
                        },
                    },
                },
                "column_mapping": {
                    "type": "object",
                    "description": (
                        "For a CSV whose header this does not recognise: which columns "
                        "hold the date, sport and duration, plus duration_unit (seconds, "
                        "minutes, hours or hh:mm:ss) and, if you name a distance column, "
                        "distance_unit (km, m or mi). Read the units off the file's own "
                        "header; do not assume them."
                    ),
                    "properties": {
                        "date": {"type": "string"},
                        "sport": {"type": "string"},
                        "duration": {"type": "string"},
                        "duration_unit": {"type": "string"},
                        "distance": {"type": ["string", "null"]},
                        "distance_unit": {"type": ["string", "null"]},
                        "external_id": {"type": ["string", "null"]},
                        "note": {"type": ["string", "null"]},
                    },
                },
                "source_name": {
                    "type": ["string", "null"],
                    "description": (
                        "What the athlete calls this upload, kept as the provenance the "
                        "coach reads -- for example 'Garmin Connect 2019-2026'."
                    ),
                },
                "resolutions": {
                    "type": "array",
                    "description": (
                        "Answers to a previous call's needs_confirmation, sent with that "
                        "same payload again."
                    ),
                    "items": {
                        "type": "object",
                        "required": ["conflict_id", "resolution"],
                        "properties": {
                            "conflict_id": {"type": "string"},
                            "resolution": {
                                "type": "string",
                                "enum": list(_IMPORT_RESOLUTIONS),
                                "description": (
                                    "same_session when the athlete says the upload "
                                    "describes a session already on record; "
                                    "separate_session when it is another session that day."
                                ),
                            },
                        },
                    },
                },
            },
        },
    ),
    Tool(
        name="retractAthleteRecord",
        kind="athlete_record_retract",
        output_schema=_RETRACT_OUTPUT,
        # candidates[].started_at stays: a repeat call copies it to disambiguate.
        redactions=_ENVELOPE_REDACTIONS
        + _EVIDENCE_VERSION_REDACTION
        + (
            ("removed", "report_id"),
            ("removed", "measurement_id"),
            ("removed", "summary_id"),
            ("removed", "state_id"),
            ("removed", "dedup_keys"),
            ("removed", "recorded_at"),
        ),
        # Destructive since #154, and now no longer the only one: removing a record and
        # overwriting one both leave the athlete's earlier statement unreachable, which
        # is the line the specification actually draws. What still makes this one worth
        # singling out is that it is the only tool whose *purpose* is to leave nothing
        # behind. Idempotent because a repeat -- or a retraction that finds nothing left
        # -- converges rather than erroring.
        annotations=_hints(
            "Take back an athlete-reported record",
            read_only=False,
            destructive=True,
            idempotent=True,
            affects_intervals=False,
        ),
        description=(
            "Call when the athlete states a stored self-reported record should not "
            "stand -- 其實那天沒練 / that entry was wrong. This is removal, not "
            "correction: a correction re-sends the matching record tool instead. "
            "Restating the same record later re-creates it."
        ),
        input_schema={
            "type": "object",
            "required": ["kind"],
            "properties": {
                "kind": {
                    "type": "string",
                    "enum": [
                        "strength_execution",
                        "body_measurement",
                        "activity_summary",
                        "subjective_state",
                        "long_term_goal",
                        "training_preference",
                    ],
                    "description": "Which stored record to take back.",
                },
                "exercise": {
                    "type": "string",
                    "description": (
                        "The movement exactly as recorded. Required when kind is "
                        "strength_execution."
                    ),
                },
                "sport": {
                    "type": "string",
                    "enum": [sport for sport in _SPORTS if sport != "rest"],
                    "description": (
                        "The sport exactly as recorded. Required when kind is "
                        "activity_summary."
                    ),
                },
                "metric": {
                    "type": "string",
                    "description": (
                        "The goal exactly as recorded. Required when kind is "
                        "long_term_goal, which is keyed by name and carries no date."
                    ),
                },
                "topic": {
                    "type": "string",
                    "description": (
                        "The habit exactly as recorded. Required when kind is "
                        "training_preference, which is keyed by name and carries no date."
                    ),
                },
                "date": {
                    "type": "string",
                    "description": (
                        "The day the record was made against, as an ISO date. Optional; "
                        "defaults to today in the athlete's own timezone. May not be in "
                        "their future."
                    ),
                },
                "started_at": {
                    "type": ["string", "null"],
                    "description": (
                        "Which session, when a day holds more than one of that sport -- "
                        "an uploaded history can. Send it only after a previous call came "
                        "back with candidates, copying the started_at of the one the "
                        "athlete meant."
                    ),
                },
                "timezone": _TIMEZONE_PROPERTY,
            },
        },
    ),
    Tool(
        name="confirmPrescribedStrength",
        kind="strength_prescribed_confirm",
        output_schema=_STRENGTH_CONFIRM_OUTPUT,
        redactions=_ENVELOPE_REDACTIONS
        + _EVIDENCE_VERSION_REDACTION
        + (
            ("movements", "*", "report_id"),
            ("movements", "*", "report", "report_id"),
            ("movements", "*", "report", "recorded_at"),
            ("movements", "*", "replaced", "report_id"),
            ("movements", "*", "replaced", "recorded_at"),
        ),
        # Destructive for a reason its own name hides: it writes through the same
        # `_upsert_strength_reports` as recordStrengthExecution, one report per
        # (date, exercise). So confirming a session the athlete had already reported
        # movement by movement overwrites what they typed with what the plan prescribed.
        annotations=_hints(
            "Record a prescribed strength session as done",
            read_only=False,
            destructive=True,
            idempotent=True,
            affects_intervals=False,
        ),
        description=(
            "Call when the athlete says a planned strength session was completed. Send "
            "session_id and only named deviations; unmentioned sets stay prescribed. One "
            "step, does not modify PlanState, and is idempotent. It writes one report per "
            "movement for that session's day, replacing any earlier report for the same "
            "movement and day. Use recordStrengthExecution for unplanned, skipped, or "
            "swapped movements."
        ),
        input_schema={
            "type": "object",
            "required": ["session_id"],
            "properties": {
                "timezone": _TIMEZONE_PROPERTY,
                "session_id": {
                    "type": "string",
                    "description": (
                        "The strength session in the current week the athlete is "
                        "confirming, from startCoachSession's plan_state. Its scheduled "
                        "date is the day the sets are recorded against; a session still "
                        "in the athlete's future is refused."
                    ),
                },
                "deviations": {
                    "type": "array",
                    "description": (
                        "One entry per set that differed from the prescription. Send "
                        "nothing here when the athlete said it went as planned. Each "
                        "entry names the movement and the set, plus only the "
                        "measurements that differed -- everything else is recorded as "
                        "prescribed."
                    ),
                    "items": {
                        "type": "object",
                        "required": ["exercise", "set"],
                        "properties": {
                            "exercise": {
                                "type": "string",
                                "description": (
                                    "The movement as the session prescribes it. Must be "
                                    "one this session actually holds."
                                ),
                            },
                            "set": {
                                "type": "integer",
                                "minimum": 1,
                                "description": (
                                    "Which set of that movement differed, counting from "
                                    "1. Must be within the number of sets prescribed."
                                ),
                            },
                            "reps": {
                                "type": ["integer", "null"],
                                "description": (
                                    "Reps actually completed, when they differed."
                                ),
                            },
                            "weight_kg": {"type": ["number", "null"]},
                            "assist_kg": {"type": ["number", "null"]},
                            "rpe": {"type": ["number", "null"]},
                        },
                    },
                },
            },
        },
    ),
    Tool(
        name="prepareCoachDecision",
        kind="decision_prepare",
        output_schema=_DECISION_PREPARE_OUTPUT,
        redactions=_ENVELOPE_REDACTIONS,
        annotations=_hints(
            "Preview a plan change",
            read_only=True,
            destructive=False,
            idempotent=True,
            affects_intervals=False,
        ),
        description=(
            "Call with one small change_request whenever the plan should move -- a "
            "weekly change, or this account's first plan. Returns the exact before/after "
            "values to show the athlete before asking for one confirmation, and writes "
            "nothing. After startCoachSession returned no_plan_state, send only "
            "change_request, with every session carrying operation \"add\"."
        ),
        input_schema={
            "type": "object",
            "required": ["change_request"],
            "properties": {
                "plan_id": {
                    "type": "string",
                    "description": (
                        "The plan_id from startCoachSession. Omit for a first plan: "
                        "there is no plan yet to name."
                    ),
                },
                "plan_version": {
                    "type": "integer",
                    "description": (
                        "The plan_version from startCoachSession. Omit for a first plan."
                    ),
                },
                "context": {
                    "type": "object",
                    "additionalProperties": True,
                    "description": (
                        "The CoachContext returned by startCoachSession. Opaque -- pass "
                        "back verbatim. Omit for a first plan, where startCoachSession "
                        "returns no context to pass."
                    ),
                },
                "red_flags": {
                    "type": "object",
                    "description": (
                        "For a first plan only, since no context exists to carry them: "
                        "symptoms the athlete stated in this turn -- true, false, or "
                        "omitted for unassessed. Never infer a value they did not give. "
                        "An explicitly true symptom limits their own today to rest, so "
                        "a first week that trains today is refused rather than written."
                    ),
                    "properties": _RED_FLAG_PROPERTIES,
                },
                "change_request": _COACH_CHANGE_REQUEST,
            },
        },
    ),
    Tool(
        name="applyCoachDecision",
        kind="decision_apply",
        output_schema=_DECISION_APPLY_OUTPUT,
        # The DecisionEvent id seeds the store's own commit slug; nothing conversational
        # takes it back.
        redactions=_ENVELOPE_REDACTIONS + (("event_id",),),
        # Not destructive, which is a real claim rather than a default: a plan change
        # appends a version to the commit chain and the one it replaces stays readable,
        # so unlike the record tools below nothing an athlete had becomes unreachable.
        # Not idempotent either -- the proposal is bound to the plan version it was
        # previewed against, so a second send does not repeat the first, it is refused.
        annotations=_hints(
            "Apply the previewed plan change",
            read_only=False,
            destructive=False,
            idempotent=False,
            affects_intervals=False,
        ),
        description=(
            "Call immediately after the athlete confirms the preview from "
            "prepareCoachDecision, with the identical context and change_request plus "
            "the returned proposal, to commit the new PlanState version. For a first "
            "plan, resend exactly what you sent then -- still no plan_id."
        ),
        input_schema={
            "type": "object",
            "required": ["change_request", "proposal"],
            "properties": {
                "plan_id": {
                    "type": "string",
                    "description": "Omit for a first plan, exactly as at preview time.",
                },
                "plan_version": {"type": "integer"},
                "context": {
                    "type": "object",
                    "additionalProperties": True,
                    "description": (
                        "The exact same CoachContext passed to prepareCoachDecision."
                    ),
                },
                "red_flags": {
                    "type": "object",
                    "description": (
                        "The same red_flags sent to prepareCoachDecision, for a first "
                        "plan. Send what the athlete has said by now: a symptom "
                        "reported between the preview and this call still refuses a "
                        "first week that trains today."
                    ),
                    "properties": _RED_FLAG_PROPERTIES,
                },
                "change_request": _RESEND_CHANGE_REQUEST,
                "proposal": {
                    "type": "string",
                    "description": (
                        "The proposal returned by prepareCoachDecision, unchanged."
                    ),
                },
                "confirmed": {
                    "type": "boolean",
                    "description": (
                        "Must be true whenever prepareCoachDecision returned "
                        "confirmation_required. Set only after the athlete has "
                        "confirmed the preview."
                    ),
                },
            },
        },
    ),
    Tool(
        name="prepareWorkoutDelivery",
        kind="delivery_prepare",
        output_schema=_DELIVERY_PREPARE_OUTPUT,
        # Redactions touch the human-facing preview rows only. `delivery_set` is not on
        # any path here and must never be: its exact bytes are what `proposal_hash`
        # covers and what applyWorkoutDelivery's approval binding verifies.
        redactions=_ENVELOPE_REDACTIONS
        + (
            ("proposal_id",),
            ("preview", "*", "owned_external_id"),
            ("preview", "*", "proposal_hash"),
        ),
        # Reads the provider prerequisites an exact preview needs (Run threshold HR and
        # sport settings) and writes nothing anywhere -- the write it previews belongs
        # to applyWorkoutDelivery, which is the one tool that answers yes.
        annotations=_hints(
            "Preview the workouts that would reach the calendar",
            read_only=True,
            destructive=False,
            idempotent=True,
            affects_intervals=False,
        ),
        description=(
            "Call to build the exact preview of the selected sessions before asking the "
            "athlete for one delivery confirmation; writes nothing. If a pace workout "
            "needs a missing Intervals Run threshold pace, settings_changes shows the "
            "narrow correction covered by that same confirmation. Set withdraw: true "
            "to preview removing superseded delivered workouts instead, when a "
            "confirmed change left one on the calendar -- session_ids then names "
            "sessions whose execution.superseded_external_id names an Intervals event "
            "the current plan no longer describes."
        ),
        input_schema={
            "type": "object",
            "required": ["plan_id", "plan_version", "session_ids"],
            "properties": {
                "plan_id": {"type": "string"},
                "plan_version": {"type": "integer"},
                "session_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "session_id values from the current PlanState's week.sessions to "
                        "prepare for delivery -- or, when withdraw is true, session_id "
                        "values whose execution.superseded_external_id names an "
                        "Intervals event the current plan no longer describes."
                    ),
                },
                "withdraw": {
                    "type": "boolean",
                    "description": (
                        "Defaults to false. True previews removing superseded delivered "
                        "workouts from Intervals instead of delivering new ones."
                    ),
                },
            },
        },
    ),
    Tool(
        name="applyWorkoutDelivery",
        kind="delivery_apply",
        output_schema=_DELIVERY_APPLY_OUTPUT,
        # What a partial retry needs stays: status, unresolved, attempt_open, and the
        # echoed proposal_hash beside the delivery_set the model already holds. Receipt
        # and provider event ids are the store's record, and the session view serves the
        # per-session delivery evidence on the next read.
        redactions=_ENVELOPE_REDACTIONS
        + (
            ("receipt_id",),
            ("delivered", "*", "external_id"),
            ("delivered", "*", "owned_external_id"),
            ("withdrawn", "*", "external_id"),
            ("state_update", "event_id"),
            ("state_update", "external_ids"),
        ),
        # Destructive because a session already on the athlete's calendar is replaced
        # in place, or a superseded one is removed outright; idempotent because
        # retrying the identical set -- deliver or withdraw -- is how a partial
        # delivery or a partial withdrawal converges -- the same facts the
        # orchestration prompt states in prose, so a client that reads only
        # annotations still retries instead of building a second set.
        annotations=_hints(
            "Apply the confirmed delivery or withdrawal to Intervals",
            read_only=False,
            destructive=True,
            idempotent=True,
            affects_intervals=True,
        ),
        description=(
            "Call immediately after the athlete confirms the preview from "
            "prepareWorkoutDelivery, with the same delivery_set and proposal_hash "
            "unchanged, to publish or withdraw -- whichever direction "
            "prepareWorkoutDelivery was called for. A confirmed settings correction is "
            "written and read back before any workout. Only events this product wrote "
            "are ever removed."
        ),
        input_schema={
            "type": "object",
            "required": ["delivery_set", "proposal_hash", "confirmed"],
            "properties": {
                "delivery_set": {
                    "type": "object",
                    "additionalProperties": True,
                    "description": (
                        "The exact delivery_set returned by prepareWorkoutDelivery, "
                        "unchanged."
                    ),
                },
                "proposal_hash": {
                    "type": "string",
                    "description": (
                        "The exact proposal_hash returned by prepareWorkoutDelivery, "
                        "unchanged."
                    ),
                },
                "confirmed": {
                    "type": "boolean",
                    "description": (
                        "Must be true. Set only after the athlete has confirmed the "
                        "preview."
                    ),
                },
                "timezone": {
                    "type": "string",
                    "description": (
                        "Overrides the athlete's stored timezone for this call only. "
                        "Only affects the withdraw direction, where it decides which "
                        "days count as already past and are therefore never removed; "
                        "ignored when applying a delivery. Omit it to use their stored "
                        "one."
                    ),
                },
            },
        },
    ),
    Tool(
        name="clearDeliveryAttempt",
        kind="delivery_attempt_clear",
        output_schema=_ATTEMPT_CLEAR_OUTPUT,
        redactions=_ENVELOPE_REDACTIONS + (("abandoned", "*", "external_id"),),
        # Destructive in the one sense that matters here: it abandons the product's own
        # record of Intervals writes nobody has reconciled, and nothing recovers it.
        annotations=_hints(
            "Abandon an unfinished delivery record",
            read_only=False,
            destructive=True,
            idempotent=True,
            affects_intervals=False,
        ),
        description=(
            "Call only for delivery.unresolved_delivery when the identical set cannot be "
            "retried and the athlete confirmed after checking Intervals. Requires "
            "confirmation; clears the pending attempt but does not reconcile Intervals "
            "or modify PlanState."
        ),
        input_schema={
            "type": "object",
            "required": ["attempt_id", "confirmed"],
            "properties": {
                "attempt_id": {
                    "type": "string",
                    "description": (
                        "The exact attempt_id from delivery.unresolved_delivery in the "
                        "most recent startCoachSession. If the store now holds a "
                        "different reservation the request is refused rather than "
                        "clearing the wrong one."
                    ),
                },
                "confirmed": {
                    "type": "boolean",
                    "description": (
                        "Must be true. Set only after telling the athlete which sessions "
                        "are unresolved and hearing that they have checked their "
                        "Intervals calendar."
                    ),
                },
            },
        },
    ),
    Tool(
        name="exportOwnerData",
        kind="data_export",
        # No redactions on the three account-lifecycle tools: an archive's timestamps,
        # version tags and receipt are part of what the athlete is owed, not metadata
        # beside it.
        output_schema=_DATA_EXPORT_OUTPUT,
        annotations=_hints(
            "Give the athlete a copy of their own data",
            read_only=True,
            destructive=False,
            idempotent=True,
            affects_intervals=False,
        ),
        description=(
            "Call when the athlete asks what this product holds about them, or for a "
            "copy of it. Returns their plan history, decisions and reported evidence, "
            "and never a credential, a fingerprint, or another athlete's data. Takes no "
            "input: the connection decides whose archive this is."
        ),
        # No properties, for the reason in the description: an athlete identifier here
        # would be the field a cross-owner export would have to travel in.
        input_schema={"type": "object", "properties": {}},
    ),
    Tool(
        name="prepareOwnerDeletion",
        kind="deletion_prepare",
        output_schema=_DELETION_PREPARE_OUTPUT,
        annotations=_hints(
            "Preview what deleting this account removes",
            read_only=True,
            destructive=False,
            idempotent=True,
            affects_intervals=False,
        ),
        description=(
            "Call when the athlete asks to delete their data, to show exactly what would "
            "go and what deletion cannot reach, before asking for one confirmation. "
            "Writes nothing."
        ),
        input_schema={"type": "object", "properties": {}},
    ),
    Tool(
        name="applyOwnerDeletion",
        output_schema=_DELETION_APPLY_OUTPUT,
        kind="deletion_apply",
        # The only tool here that destroys rather than replaces. Idempotent because a
        # repeat finds nothing left, which is also how a half-finished erasure finishes.
        annotations=_hints(
            "Permanently erase this account",
            read_only=False,
            destructive=True,
            idempotent=True,
            affects_intervals=False,
        ),
        description=(
            "Call immediately after the athlete confirms the preview from "
            "prepareOwnerDeletion, with the returned proposal. Permanently erases this "
            "account's plan, history and reported evidence; it cannot be undone, and it "
            "removes nothing from their Intervals calendar or authorization."
        ),
        input_schema={
            "type": "object",
            "required": ["proposal", "confirmed"],
            "properties": {
                "proposal": {
                    "type": "string",
                    "description": (
                        "The exact proposal returned by prepareOwnerDeletion, unchanged. "
                        "An account that has gained a plan version or a reported session "
                        "since that preview is refused rather than erased."
                    ),
                },
                "confirmed": {
                    "type": "boolean",
                    "description": (
                        "Must be true. Set only after showing the athlete the preview, "
                        "including what deletion cannot reach, and hearing them agree."
                    ),
                },
            },
        },
    ),
)

TOOLS_BY_NAME: dict[str, Tool] = {tool.name: tool for tool in TOOLS}


def tool_catalogue_sha256(tools: Sequence[Tool] = TOOLS) -> str:
    """One digest over the catalogue ``tools/list`` actually serves.

    Every field a client reads is inside it -- name, title, description, input schema and
    every annotation -- so a renamed tool, a loosened schema, or a ``readOnlyHint`` that
    flipped moves the release identity rather than shipping unnoticed under an unchanged
    one. The catalogue is *what* is served, not a file that describes it, so the only way
    to hash it is to build it.

    Sorted by name and by key: a client indexes the catalogue by tool name, so the order
    two gateways happen to list the same tools in is not a difference between them, and
    binding it would report one where there is none.
    """
    return sha256_text(
        json.dumps(
            sorted((tool.descriptor() for tool in tools), key=lambda entry: entry["name"]),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    )


# --------------------------------------------------------------------------------------
# JSON-RPC 2.0
# --------------------------------------------------------------------------------------


def _result(message_id: Any, result: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": message_id, "result": result}


def _error(message_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": message_id, "error": {"code": code, "message": message}}


def _negotiated_version(requested: Any) -> str:
    """Answer with the client's version when it is one this server speaks, else our own.

    A client that cannot live with the answer disconnects, which is the protocol's own
    resolution. Guessing agreement would be worse: it would leave the client believing a
    removed feature is still available.
    """
    return requested if requested in SUPPORTED_PROTOCOL_VERSIONS else PROTOCOL_VERSION


def _text_content(payload: dict[str, Any]) -> dict[str, Any]:
    """One tool response, rendered exactly as the gateway's own JSON body is.

    ``ensure_ascii=False`` so the athlete's own language survives the encoding rather
    than reaching the model as escape sequences.
    """
    return {"type": "text", "text": json.dumps(payload, ensure_ascii=False)}


def _call_tool(
    message_id: Any, params: Any, call_tool: Callable[[str, dict[str, Any]], dict[str, Any]]
) -> dict[str, Any]:
    if not isinstance(params, dict):
        return _error(message_id, INVALID_PARAMS, "params must be an object")
    name = params.get("name")
    tool = TOOLS_BY_NAME.get(name) if isinstance(name, str) else None
    if tool is None:
        # A name this server does not serve is a protocol-level mistake by the client, not
        # a coaching refusal: there is no tool whose result it could be.
        return _error(message_id, INVALID_PARAMS, f"unknown tool: {name!r}")
    arguments = params.get("arguments")
    if arguments is None:
        arguments = {}
    if not isinstance(arguments, dict):
        return _error(message_id, INVALID_PARAMS, "arguments must be an object")
    try:
        payload = call_tool(tool.kind, arguments)
    except ToolCallBlocked as blocked:
        # The gateway's own refusal body, through the same projection, so the model reads
        # the same `status: blocked` and error code the gateway's own body carries.
        # No `structuredContent` here: `outputSchema` describes this tool's result, a
        # refusal is not one, and a validating client must not be handed a refusal shaped
        # as if it were.
        return _result(
            message_id,
            {"content": [_text_content(_redact(blocked.payload, tool.redactions))], "isError": True},
        )
    projected = _redact(payload, tool.redactions)
    # Both members carry the same projected object: `structuredContent` is what
    # 2025-06-18 obliges a tool with an `outputSchema` to return, and the text block is
    # the same JSON serialized for clients that read only `content`. One projection,
    # serialized once -- never a fuller copy on one member than the other.
    return _result(
        message_id,
        {"content": [_text_content(projected)], "structuredContent": projected},
    )


def _get_prompt(message_id: Any, params: Any) -> dict[str, Any]:
    """Serve one of the prompts this server has, or say plainly that a name is not one."""
    if not isinstance(params, dict):
        return _error(message_id, INVALID_PARAMS, "params must be an object")
    name = params.get("name")
    served = orchestration.PROMPTS.get(name) if isinstance(name, str) else None
    if served is None:
        return _error(message_id, INVALID_PARAMS, f"unknown prompt: {name!r}")
    return _result(message_id, served[1]())


def handle(
    raw: bytes,
    *,
    call_tool: Callable[[str, dict[str, Any]], dict[str, Any]],
    server_version: str,
) -> tuple[int, dict[str, Any] | None]:
    """Answer one MCP message: ``(HTTP status, JSON-RPC body or None)``.

    ``None`` means the message was a notification, which has no response at all -- the
    caller sends the status and no body.

    ``call_tool`` is the caller's already-authenticated dispatch into
    ``CoachGateway.route``; it raises ``ToolCallBlocked`` for a refusal. Nothing in this
    function knows which athlete it is serving, which is the point.
    """
    try:
        message = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return 400, _error(None, PARSE_ERROR, "request body must be UTF-8 JSON-RPC")
    if isinstance(message, list):
        # Removed from the protocol in 2025-06-18. Saying so plainly beats answering the
        # first element and silently dropping the rest.
        return 400, _error(None, INVALID_REQUEST, "JSON-RPC batching is not supported")
    if (
        not isinstance(message, dict)
        or message.get("jsonrpc") != "2.0"
        or not isinstance(message.get("method"), str)
    ):
        return 400, _error(None, INVALID_REQUEST, "not a JSON-RPC 2.0 request")

    method = message["method"]
    # A notification is defined by the *absence* of the member, not by a null id.
    if "id" not in message:
        # Every notification this server can receive -- `notifications/initialized` and
        # anything a future client sends -- is accepted and answered with nothing. There
        # is no per-connection state for one to change.
        return 202, None

    message_id = message["id"]
    if method == "initialize":
        params = message.get("params")
        requested = params.get("protocolVersion") if isinstance(params, dict) else None
        return 200, _result(
            message_id,
            {
                "protocolVersion": _negotiated_version(requested),
                # Tools, and the two prompts that say how to sequence them and how to
                # coach. No resources, sampling or logging: each would be a second way to
                # reach the same state. A prompt is not that -- it reaches no state at
                # all (issue #125).
                "capabilities": {"tools": {}, "prompts": {}},
                # The one layer that does not wait to be asked for. A prompt is
                # user-controlled by specification -- a client is expected to offer it for
                # explicit selection, and Claude Code surfaces each as a slash command --
                # so serving one is not delivering it. `instructions` is the surface the
                # specification defines for text a host may put in front of its model
                # without anybody choosing it, which is exactly what sequencing is: a
                # client that drives these operations without it can write to an
                # athlete's calendar without the confirmation this product is built on.
                #
                # `MAY` is as strong as the specification gets, so this is not a
                # guarantee either -- it is the difference between "no host can be
                # expected to have it" and "a host that honours the field does".
                "instructions": orchestration.instructions(),
                "serverInfo": {"name": SERVER_NAME, "version": server_version},
            },
        )
    if method == "ping":
        # Base-protocol liveness check; an unanswered ping reads as a dead connection.
        return 200, _result(message_id, {})
    if method == "tools/list":
        return 200, _result(message_id, {"tools": [tool.descriptor() for tool in TOOLS]})
    if method == "tools/call":
        return 200, _call_tool(message_id, message.get("params"), call_tool)
    if method == "prompts/list":
        return 200, _result(
            message_id,
            {"prompts": [descriptor() for descriptor, _ in orchestration.PROMPTS.values()]},
        )
    if method == "prompts/get":
        return 200, _get_prompt(message_id, message.get("params"))
    return 200, _error(message_id, METHOD_NOT_FOUND, f"unknown method: {method!r}")
