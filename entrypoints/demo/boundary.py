"""What a demo turn is allowed to do, decided here and nowhere else.

The demo may read, reason, explore, and go as far as a plan-change *preview*. It may not
write anything: no provider call, no calendar delivery, no OAuth, no account mutation, no
deletion, and no change to any PlanState -- not the fixture's, not another session's, not
an owner's, which it has no path to in the first place.

The refusal is written down rather than merely implied. A boundary that exists only as
"we did not wire that up" is one a later change removes without noticing, and it gives a
model that asks for a write nothing to read back. So every act the demo can be asked for
goes through ``dispatch`` and lands in exactly one of three places: a reader, the preview,
or a refusal that says what this is. ``REFUSED_ACTS`` names the product's real write
operations on purpose -- the model has met them, it will ask for them by name, and being
told "this is a playground" by name is a better answer than "no such tool".

Nothing here can be relaxed by a request. The allowed set is a module constant, the
refusal text is not model-supplied, and a name in neither set is refused the same way.
"""

from __future__ import annotations

import copy
import datetime as dt
from typing import Any

from garmin_coach_loop import context_view, plan_change

from . import fixture


PLAYGROUND_NOTE = (
    "This is the Long Run Hybrid Coach playground. It reads one synthetic athlete and can "
    "explore and preview changes, but it cannot save a plan, write to a calendar, connect "
    "an account, or change anything that outlasts this conversation. Say what the change "
    "would be and what it costs; do not say it has been made."
)

# snake_case on purpose. Every operation this product actually serves is camelCase, so a
# name in this shape cannot be mistaken for one of them -- in a log line, in a transcript,
# or in a document. The demo serves none of the product's operations and never will.
READ_EVIDENCE = "read_demo_evidence"
PREVIEW_PLAN_CHANGE = "preview_demo_plan_change"

ALLOWED_ACTS: tuple[str, ...] = (READ_EVIDENCE, PREVIEW_PLAN_CHANGE)

# The product's whole operation catalogue, named so a refusal can be specific. Not a
# permission list: none of it is reachable from this process, and the names exist only so
# that a model reaching for one is answered in its own vocabulary rather than told the name
# does not exist. `tests/test_demo_boundary.py` holds this equal to the served catalogue, so
# a twenty-fourth operation cannot land and be silently absent from here.
REFUSED_ACTS: tuple[str, ...] = (
    "applyCoachDecision",
    "applyWorkoutDelivery",
    "prepareWorkoutDelivery",
    "prepareCoachDecision",
    "clearDeliveryAttempt",
    "confirmActivityMatch",
    "confirmPrescribedStrength",
    "confirmSessionNotTrained",
    "exportOwnerData",
    "importAthleteHistory",
    "inspectIntervalsPermissions",
    "recordActivitySummary",
    "recordAthleteAvailability",
    "recordAthleteProfile",
    "recordBodyMeasurement",
    "recordLongTermGoal",
    "recordStrengthExecution",
    "recordSubjectiveState",
    "recordTrainingPreference",
    "retractAthleteRecord",
    "startCoachSession",
    "getCoachState",
    "readCoachEvidence",
)


class DemoWriteRefused(Exception):
    """An act the demo will not perform, carrying the sentence a caller is answered with."""

    def __init__(self, act: str, reason: str) -> None:
        super().__init__(reason)
        self.act = act
        self.reason = reason


def refusal(act: str) -> dict[str, Any]:
    """The refusal, in the shape a tool result takes.

    One shape for every refused act, whether it is a named write or a name this service
    has never heard of: telling the two apart would let a caller enumerate the surface,
    and neither is going to happen either way.
    """
    if act in REFUSED_ACTS:
        reason = (
            f"{act} is one of the coach's real operations against a connected athlete's "
            "account, plan and calendar. The demo has no account, no provider connection "
            "and no store, so there is nothing for it to read or write."
        )
    else:
        reason = f"{act} is not something the demo can do."
    return {
        "status": "refused",
        "code": "demo_write_forbidden",
        "act": act,
        "reason": reason,
        "playground": PLAYGROUND_NOTE,
        "allowed": list(ALLOWED_ACTS),
    }


def _read_evidence(arguments: dict[str, Any]) -> dict[str, Any]:
    """More of the same fixture, through the gateway's own group projection."""
    try:
        fields, holds = fixture.evidence_slice(arguments.get("read"))
    except context_view.EvidenceGroupError as error:
        return {"status": "invalid", "code": "unknown_evidence_group", "reason": str(error)}
    return {"status": "passed", "evidence": fields, "groups": holds}


def _preview_plan_change(
    arguments: dict[str, Any], *, before: dict[str, Any], issued_at: dt.datetime
) -> dict[str, Any]:
    """One change request projected against this session's own copy of the plan.

    ``plan_change.project_change_request`` is the product's projector, used exactly as the
    gateway uses it and with the same validator behind it -- a request that would be
    refused for a real athlete is refused here, in the same words. What does not happen is
    everything after the projection: no DecisionEvent is stored, no version is spent, no
    confirmation is offered, and the plan handed in comes back untouched.
    """
    try:
        projected = plan_change.project_change_request(
            copy.deepcopy(before),
            arguments.get("change_request"),
            context=fixture.context(),
            issued_at=issued_at,
            language=fixture.LANGUAGE,
        )
    except plan_change.ChangeRequestError as error:
        return {"status": "invalid", "code": "change_request_invalid", "reason": str(error)}
    return {
        "status": "preview_only",
        "preview": projected["preview"],
        "material_change": projected["material_change"],
        "unknowns": list(projected["decision_event"].get("unknowns") or []),
        "not_applied": (
            "Nothing was saved. This is what the change would look like, not a change that "
            "has been made."
        ),
    }


def dispatch(
    act: str,
    arguments: Any,
    *,
    before: dict[str, Any],
    issued_at: dt.datetime,
) -> dict[str, Any]:
    """Run one requested act, or refuse it.

    The single door. Every act a model asks for arrives here, and an act that is not in
    ``ALLOWED_ACTS`` leaves through ``refusal`` -- there is no branch that reaches a write,
    because there is no write in this process to reach.
    """
    if act not in ALLOWED_ACTS:
        return refusal(act)
    if not isinstance(arguments, dict):
        return {
            "status": "invalid",
            "code": "arguments_invalid",
            "reason": f"{act} takes a JSON object of arguments",
        }
    if act == READ_EVIDENCE:
        return _read_evidence(arguments)
    return _preview_plan_change(arguments, before=before, issued_at=issued_at)


def tool_definitions() -> list[dict[str, Any]]:
    """The two acts, as the Responses API declares a function tool.

    Descriptions stay short. They are read by the model on every turn, and the judgment
    they would otherwise carry belongs to the served training prompt, which the demo hands
    over whole (AGENTS.md 11, 13).
    """
    return [
        {
            "type": "function",
            "name": READ_EVIDENCE,
            "description": (
                "Load evidence groups this turn did not already receive. Read-only. Names "
                "the same groups evidence_index lists."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "read": {
                        "type": "array",
                        "items": {"type": "string", "enum": list(context_view.ALL_GROUPS)},
                        "description": "Evidence groups to load.",
                    }
                },
                "required": ["read"],
                "additionalProperties": False,
            },
        },
        {
            "type": "function",
            "name": PREVIEW_PLAN_CHANGE,
            "description": (
                "Project one change request against this conversation's copy of the plan "
                "and return the preview. Nothing is saved, and no confirmation follows: "
                "the demo cannot apply a change."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "change_request": {
                        "type": "object",
                        "description": (
                            "The same change_request shape prepareCoachDecision takes: "
                            "decision_scope, summary, reason_codes, evidence, goal_effect, "
                            "next_review_condition, unknowns, and optionally goal, cycle, "
                            "week, athlete_baseline and sessions."
                        ),
                    }
                },
                "required": ["change_request"],
                "additionalProperties": False,
            },
        },
    ]
