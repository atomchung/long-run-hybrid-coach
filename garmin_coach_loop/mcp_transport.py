"""Model Context Protocol transport over the Coach Gateway's own routes.

An MCP client -- claude.ai's custom connector, Codex, any other -- speaks JSON-RPC 2.0
over one HTTP endpoint instead of one path per operation. That is the only difference
this module introduces. Every tool below ends in ``CoachGateway.route``, the same
dispatch the ``/v1/coach/*`` paths use, so the product still holds exactly one
validator, one store, one delivery boundary and one identity check: a new entry changes
data sources and operator tooling, never coaching capability (AGENTS.md invariant 10).

Three consequences shape the code:

- **Nothing here touches product state.** This module imports no store, delivery or
  validation module and holds no owner, token or path. It reads a JSON-RPC message,
  names a route kind, and renders whatever the gateway handed back.
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
from typing import Any, Callable


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
# Static, self-contained JSON Schema. They carry the same field names, required sets and
# types as the OpenAPI request bodies the Custom GPT entry uses -- one command surface,
# two descriptions of it -- which tests/test_mcp_gateway.py holds them to. They are
# written out rather than derived from that file because a runtime that parsed YAML
# would need a YAML parser, and this package stays stdlib-only.
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
_SPORTS = ["running", "strength", "mobility", "recovery", "rest"]
_BODY_STRESS = ["lower", "upper", "full", "systemic"]
_COSTS = ["easy", "moderate", "hard"]
_PRIORITIES = ["anchor", "flexible", "optional"]

_INITIAL_SESSION: dict[str, Any] = {
    "type": "object",
    "description": (
        "One session of the first week. The gateway names it and derives whether it is "
        "hard and whether it can be delivered."
    ),
    "required": [
        "sport",
        "scheduled_date",
        "purpose",
        "adaptation",
        "body_stress",
        "cost",
        "priority",
        "planned_minutes",
        "plan",
        "fallback",
    ],
    "properties": {
        "sport": {"type": "string", "enum": _SPORTS},
        "scheduled_date": {
            "type": "string",
            "description": (
                "ISO date, inside the first week (cycle start through cycle start plus "
                "six days)."
            ),
        },
        "purpose": {
            "type": "string",
            "description": (
                "What the session is for, and the title a strength day reaches the "
                "athlete's watch under. Intent only, never a prescription -- a number "
                "wearing a unit (4:30/km, 5km, 80kg, 150bpm, 85%) is refused and the "
                "error names it. A digit on its own is fine. Every number the athlete "
                "executes belongs in plan."
            ),
        },
        "adaptation": {"type": "string", "enum": _ADAPTATIONS},
        "body_stress": {"type": "string", "enum": _BODY_STRESS},
        "cost": {"type": "string", "enum": _COSTS},
        "priority": {"type": "string", "enum": _PRIORITIES},
        "planned_minutes": {"type": "integer"},
        "plan": _SESSION_PLAN,
        "time_window": {
            "type": ["string", "null"],
            "description": "Optional, for example morning or evening.",
        },
        "fallback": _FALLBACK,
    },
}

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
_RESEND_INITIALIZATION_REQUEST: dict[str, Any] = {
    "type": "object",
    "description": (
        "The identical initialization_request you sent to prepareCoachInitialization, "
        "resent unchanged. Do not re-author it: the proposal binds that exact content, "
        "and any difference is refused."
    ),
}

_RESEND_CHANGE_REQUEST: dict[str, Any] = {
    "type": "object",
    "description": (
        "The identical change_request you sent to prepareCoachDecision, resent "
        "unchanged. Do not re-author it: the proposal binds that exact content, and "
        "any difference is refused."
    ),
}


_COACH_INITIALIZATION_REQUEST: dict[str, Any] = {
    "type": "object",
    "description": (
        "The first plan, carrying coaching judgment and athlete facts only. The gateway "
        "builds the PlanState from it and owns its schema, id, version, status, session "
        "ids, cycle end date, week start and delivery bookkeeping. Never send a PlanState."
    ),
    "required": ["goal", "cycle", "week_intent", "sessions", "summary", "evidence"],
    "properties": {
        "goal": {
            "type": "object",
            "required": ["outcome", "measurement_protocol"],
            "properties": {
                "outcome": {
                    "type": "string",
                    "description": "What the athlete is training for, in their own words.",
                },
                "measurement_protocol": {
                    "type": "string",
                    "description": "How they will tell at day 28 whether it worked.",
                },
            },
        },
        "cycle": {
            "type": "object",
            "description": "Where the 28 days point. The end date is derived from start.",
            "required": [
                "start",
                "primary_adaptation",
                "planned_evidence",
                "adjust_conditions",
                "stop_conditions",
            ],
            "properties": {
                "start": {
                    "type": "string",
                    "description": "ISO date the block starts. The first week starts with it.",
                },
                "primary_adaptation": {"type": "string", "enum": _ADAPTATIONS},
                "maintenance_adaptation": {
                    "type": ["string", "null"],
                    "enum": [*_ADAPTATIONS, None],
                },
                "planned_evidence": {
                    "type": "array",
                    "minItems": 1,
                    "items": {"type": "string"},
                    "description": "What the block should produce if it is working.",
                },
                "adjust_conditions": {
                    "type": "array",
                    "minItems": 1,
                    "items": {"type": "string"},
                },
                "stop_conditions": {
                    "type": "array",
                    "minItems": 1,
                    "items": {"type": "string"},
                },
            },
        },
        "week_intent": {"type": "string", "description": "What the first week is for."},
        "availability": {
            "type": "object",
            "description": (
                "When the athlete can train and with what. Echoed in the preview for "
                "them to correct; the sessions are where it takes effect."
            ),
            "properties": {
                "days": {"type": "array", "items": {"type": "string"}},
                "equipment": {"type": "array", "items": {"type": "string"}},
            },
        },
        "baselines": {
            "type": "object",
            "description": (
                "Only what the athlete actually supports. Leave out, or send null, "
                "anything they have not measured -- a missing anchor stays unknown and "
                "is reported back in unknowns. Never estimate one to fill the field, "
                "and never prescribe an exact pace, BPM or kg that no baseline here "
                "supports."
            ),
            "properties": {
                "threshold_pace_sec_per_km": {"type": ["integer", "null"]},
                "max_hr": {"type": ["integer", "null"]},
                "easy_hr_ceiling": {"type": ["integer", "null"]},
                "longest_recent_run_km": {"type": ["number", "null"]},
                "weekly_volume_km_4wk_avg": {"type": ["number", "null"]},
                "max_session_minutes": {"type": ["integer", "null"]},
                "strength_loads": {
                    "type": "array",
                    "description": (
                        "One entry per lift the athlete has a real figure for. An "
                        "assisted lift records assist_kg and leaves load_kg null."
                    ),
                    "items": {
                        "type": "object",
                        "required": ["exercise"],
                        "properties": {
                            "exercise": {"type": "string"},
                            "load_kg": {"type": ["number", "null"]},
                            "assist_kg": {"type": ["number", "null"]},
                            "scheme": {"type": ["string", "null"]},
                            "display_name": {
                                "type": "string",
                                "description": (
                                    "How the athlete says this lift, when a plan names "
                                    "it in their wording."
                                ),
                            },
                        },
                    },
                },
            },
        },
        "sessions": {
            "type": "array",
            "minItems": 1,
            "description": (
                "The first week's sessions. There is no default week; every session is "
                "one you decided on."
            ),
            "items": _INITIAL_SESSION,
        },
        "summary": {
            "type": "string",
            "description": (
                "One line saying what this plan is and why, in the athlete's own language."
            ),
        },
        "evidence": {
            "type": "array",
            "minItems": 1,
            "description": "What the athlete actually told you, and where it came from.",
            "items": {
                "type": "object",
                "required": ["field", "observation"],
                "properties": {
                    "field": {"type": "string"},
                    "observation": {"type": "string"},
                },
            },
        },
        "unknowns": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "What you could not establish. The gateway adds every unmeasured "
                "baseline to these."
            ),
        },
    },
}

_COACH_CHANGE_REQUEST: dict[str, Any] = {
    "type": "object",
    "description": (
        "One small coaching change, carrying coaching judgment only. The gateway "
        "projects it onto the current PlanState: it copies every field you did not "
        "change, and it owns the resulting PlanState and DecisionEvent, their versions, "
        "ids, hashes, timestamps, and delivery bookkeeping. Never send a PlanState or a "
        "DecisionEvent."
    ),
    "required": [
        "summary",
        "reason_codes",
        "evidence",
        "goal_effect",
        "next_review_condition",
    ],
    "properties": {
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
                "Send only when the 28-day outcome itself changes; both fields are then "
                "required."
            ),
            "required": ["outcome", "measurement_protocol"],
            "properties": {
                "outcome": {"type": "string"},
                "measurement_protocol": {"type": "string"},
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
                "easy_hr_ceiling": {"type": "integer"},
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


# --------------------------------------------------------------------------------------
# The tools
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Tool:
    """One MCP tool and the gateway route kind it dispatches to.

    ``name`` is the OpenAPI ``operationId`` of the same operation, unchanged. The two
    entries then name one capability identically, so an athlete moving between them --
    and anyone reading a transcript from either -- sees the same vocabulary.
    """

    name: str
    kind: str
    description: str
    input_schema: dict[str, Any]

    def descriptor(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "inputSchema": self.input_schema,
        }


TOOLS: tuple[Tool, ...] = (
    Tool(
        name="startCoachSession",
        kind="session",
        description=(
            "Call before answering any today, this-week, plan, or reassessment "
            "question; the returned PlanState is the only durable memory across "
            "conversations."
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
                        "unassessed. Never infer a value the athlete did not give."
                    ),
                    "properties": {
                        "pain": {"type": ["boolean", "null"]},
                        "illness": {"type": ["boolean", "null"]},
                        "chest_pain": {"type": ["boolean", "null"]},
                        "dizziness": {"type": ["boolean", "null"]},
                        "unusual_symptoms": {"type": ["boolean", "null"]},
                    },
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
            },
        },
    ),
    Tool(
        name="inspectIntervalsPermissions",
        kind="permissions",
        description=(
            "Call only when debugging a connection. Returns normalized OAuth scope "
            "names and a bounded SETTINGS read classification; never returns provider "
            "settings or credentials."
        ),
        # Takes nothing: the connected token is the whole input, and it never travels in
        # a tool argument.
        input_schema={"type": "object", "properties": {}},
    ),
    Tool(
        name="recordAthleteProfile",
        kind="profile_record",
        description=(
            "Call when the athlete says where they are or which language they want "
            "their plan in. Needs no confirmation and does not modify PlanState. Send "
            "only what they stated; the other field stays as it was. Stored once and "
            "used by every later call."
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
        kind="availability_record",
        description=(
            "Call when the athlete states available or unavailable days. Needs no "
            "confirmation and does not modify PlanState. Send only the stated week or "
            "recurring days; omitted days stay unchanged. Re-sending the same report is "
            "idempotent."
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
                    },
                },
            },
        },
    ),
    Tool(
        name="recordStrengthExecution",
        kind="strength_report",
        description=(
            "Call when the athlete reports completed strength sets. Needs no "
            "confirmation and does not modify PlanState. Send only stated values; never "
            "infer missing load or reps. Re-send the same movement/day to correct it; "
            "identical reports are idempotent."
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
        name="confirmPrescribedStrength",
        kind="strength_prescribed_confirm",
        description=(
            "Call when the athlete says a planned strength session was completed. Send "
            "session_id and only named deviations; unmentioned sets stay prescribed. "
            "Needs no confirmation, does not modify PlanState, and is idempotent. Use "
            "recordStrengthExecution for unplanned, skipped, or swapped movements."
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
        name="prepareCoachInitialization",
        kind="initialization_prepare",
        description=(
            "Call only after startCoachSession returned no_plan_state, with one small "
            "initialization_request built from what the athlete told you; returns the "
            "exact first plan to show them before asking for one confirmation, and "
            "writes nothing."
        ),
        input_schema={
            "type": "object",
            "required": ["initialization_request"],
            "properties": {"initialization_request": _COACH_INITIALIZATION_REQUEST},
        },
    ),
    Tool(
        name="initializeCoachPlan",
        kind="initialization_apply",
        description=(
            "Call immediately after the athlete confirms the preview from "
            "prepareCoachInitialization, with the identical initialization_request and "
            "the returned proposal, to create this account's PlanState."
        ),
        input_schema={
            "type": "object",
            "required": ["initialization_request", "proposal", "confirmed"],
            "properties": {
                "initialization_request": _RESEND_INITIALIZATION_REQUEST,
                "proposal": {
                    "type": "string",
                    "description": (
                        "The proposal returned by prepareCoachInitialization, unchanged."
                    ),
                },
                "confirmed": {
                    "type": "boolean",
                    "description": (
                        "Must be true. Set only after the athlete has confirmed the "
                        "preview."
                    ),
                },
            },
        },
    ),
    Tool(
        name="prepareCoachDecision",
        kind="decision_prepare",
        description=(
            "Call once a weekly change is needed, with one small change_request; "
            "returns the exact before/after values to show the athlete before asking "
            "for one confirmation, and writes nothing."
        ),
        input_schema={
            "type": "object",
            "required": ["plan_id", "plan_version", "context", "change_request"],
            "properties": {
                "plan_id": {
                    "type": "string",
                    "description": "The plan_id from startCoachSession.",
                },
                "plan_version": {
                    "type": "integer",
                    "description": "The plan_version from startCoachSession.",
                },
                "context": {
                    "type": "object",
                    "additionalProperties": True,
                    "description": (
                        "The CoachContext returned by startCoachSession. Opaque -- pass "
                        "back verbatim."
                    ),
                },
                "change_request": _COACH_CHANGE_REQUEST,
            },
        },
    ),
    Tool(
        name="applyCoachDecision",
        kind="decision_apply",
        description=(
            "Call immediately after the athlete confirms the preview from "
            "prepareCoachDecision, with the identical context and change_request plus "
            "the returned proposal, to commit the new PlanState version."
        ),
        input_schema={
            "type": "object",
            "required": [
                "plan_id",
                "plan_version",
                "context",
                "change_request",
                "proposal",
            ],
            "properties": {
                "plan_id": {"type": "string"},
                "plan_version": {"type": "integer"},
                "context": {
                    "type": "object",
                    "additionalProperties": True,
                    "description": (
                        "The exact same CoachContext passed to prepareCoachDecision."
                    ),
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
        description=(
            "Call to build the exact preview of the selected sessions before asking the "
            "athlete for one delivery confirmation; writes nothing."
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
                        "prepare for delivery."
                    ),
                },
            },
        },
    ),
    Tool(
        name="publishWorkoutDelivery",
        kind="delivery_publish",
        description=(
            "Call immediately after the athlete confirms the preview from "
            "prepareWorkoutDelivery, with the same delivery_set and proposal_hash "
            "unchanged, to publish to Intervals."
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
            },
        },
    ),
    Tool(
        name="prepareDeliveryWithdrawal",
        kind="withdrawal_prepare",
        description=(
            "Call when a confirmed change left a previously delivered workout on the "
            "calendar, to show the athlete exactly which Intervals events would be "
            "removed; writes nothing."
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
                        "session_id values whose execution.superseded_external_id names "
                        "an Intervals event the current plan no longer describes."
                    ),
                },
            },
        },
    ),
    Tool(
        name="applyDeliveryWithdrawal",
        kind="withdrawal_apply",
        description=(
            "Call immediately after the athlete confirms the preview from "
            "prepareDeliveryWithdrawal, with the same withdrawal_set and proposal_hash "
            "unchanged. Only events this product wrote are ever removed."
        ),
        input_schema={
            "type": "object",
            "required": ["withdrawal_set", "proposal_hash", "confirmed"],
            "properties": {
                "timezone": {
                    "type": "string",
                    "description": (
                        "Overrides the athlete's stored timezone for this call only. "
                        "It decides which days count as already past and are therefore "
                        "never removed; omit it to use their stored one."
                    ),
                },
                "withdrawal_set": {
                    "type": "object",
                    "additionalProperties": True,
                    "description": (
                        "The exact withdrawal_set returned by prepareDeliveryWithdrawal, "
                        "unchanged."
                    ),
                },
                "proposal_hash": {
                    "type": "string",
                    "description": (
                        "The exact proposal_hash returned by prepareDeliveryWithdrawal, "
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
            },
        },
    ),
    Tool(
        name="clearDeliveryAttempt",
        kind="delivery_attempt_clear",
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
)

TOOLS_BY_NAME: dict[str, Tool] = {tool.name: tool for tool in TOOLS}


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
    """One tool response, rendered exactly as the REST body would be.

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
        # The gateway's own refusal body, unchanged and unwrapped, so the model reads the
        # same `status: blocked` and error code the REST entry would have shown it.
        return _result(
            message_id, {"content": [_text_content(blocked.payload)], "isError": True}
        )
    return _result(message_id, {"content": [_text_content(payload)]})


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
                # Tools only. No resources, prompts, sampling or logging: every one of
                # them would be a second way to reach the same state.
                "capabilities": {"tools": {}},
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
    return 200, _error(message_id, METHOD_NOT_FOUND, f"unknown method: {method!r}")
