"""The same question, answered from everything and answered from what the coach asked for.

`evals/ab/` compares two *context builds*. This compares two *reading strategies* against
one build, which is the question issue #15 makes a release gate:

> compare the same anonymous facts/questions using the full reference and the candidate's
> actual autonomous retrieval path within each model family

The distinction matters because the candidate arm is not a payload. It is a loop: the
model says what the turn is reading for, receives that, and may expand -- by group, or
focused on one session, day or movement -- before it answers. A gate that handed the
model a pre-chosen projection would be measuring a projection, not a retrieval path, and
would pass or fail on whoever chose it.

## The two arms

``reference``  one `startCoachSession` with `read: "all"`. Every group, one result.
``candidate``  the model declares `read`, receives that projection and its
               `evidence_index`, and may call `readCoachEvidence` -- as many times as it
               judges it needs -- against the same snapshot before answering.

Both arms answer out of the *same* committed read, so nothing separates them but what
the model asked for. Reads come from `tests/coach_session_scenarios.py` and questions
from `evals/ab/suite.json`, for the reason that file gives: a second set here would be
the copy nobody edits.

## What this cannot do by itself

It builds packets and answers retrievals. It does not answer coaching questions -- a
model does, and the repository may not call one (AGENTS.md, first line). So a run is
whoever is driving it handing each packet to a model, blind, and recording the answer.
`n=1` is not a result; repeats of the same packet are what separate a difference between
arms from a difference between two samples of one arm.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

from garmin_coach_loop import context_view, orchestration


ROOT = Path(__file__).resolve().parents[2]
SUITE = ROOT / "evals" / "ab" / "suite.json"


class RetrievalGateError(RuntimeError):
    """A packet or a retrieval this harness cannot build."""


def turns() -> list[dict[str, Any]]:
    """The athlete questions, with the committed read each one is asked of."""
    return json.loads(SUITE.read_text(encoding="utf-8"))["turns"]


def whole_response(scenario: str) -> dict[str, Any]:
    """What `startCoachSession` hands back for one scenario, every group loaded."""
    from tests import coach_session_scenarios as scenarios_module

    found = next(
        (item for item in scenarios_module.scenarios() if item.name == scenario), None
    )
    if found is None:
        raise RetrievalGateError(f"no scenario named {scenario!r}")
    snapshot = scenarios_module.run(found)
    if snapshot["response"] is None:
        raise RetrievalGateError(f"scenario {scenario!r} ends in a blocked build")
    response = copy.deepcopy(snapshot["response"])
    # The snapshot omits the judgment deliberately (it is identical on every read and
    # would be thirty copies in the fixtures). A packet needs it: it is what the coach
    # is told to coach from, and an arm answering without it is answering a different
    # question than an arm answering with it.
    response["coaching_guidance"] = orchestration.training_judgment()
    return response


def project(response: dict[str, Any], read: list[str]) -> dict[str, Any]:
    """The same response as a declared read would have returned it.

    Not a summary and not a second build: `project_context` is the function the gateway
    itself calls, so what a packet shows is what the route would have sent.
    """
    groups = context_view.parse_read(read)
    context = response.get("context")
    if not isinstance(context, dict):
        raise RetrievalGateError("this response has no context to project")
    view, index = context_view.project_context(context, groups)
    projected = copy.deepcopy(response)
    projected["context"] = view
    if index is not None:
        projected["evidence_index"] = index
    plan_state = projected.get("plan_state")
    if isinstance(plan_state, dict) and not context_view.plan_is_read(groups):
        # The same rule the route applies: a turn that is not about training gets the
        # plan named and its week summarized rather than every session of the cycle.
        plan = plan_state.get("current_plan")
        if isinstance(plan, dict):
            plan_state.pop("current_plan", None)
            plan_state["week"] = plan.get("week")
            plan_state["goal"] = plan.get("goal")
    return projected


def expand(
    response: dict[str, Any], read: list[str], focus: dict[str, Any] | None = None
) -> dict[str, Any]:
    """One `readCoachEvidence` call, answered from the same snapshot."""
    context = response.get("context")
    if not isinstance(context, dict):
        raise RetrievalGateError("this response has no context to expand")
    groups = context_view.parse_read(read)
    evidence, holds = context_view.group_slice(context, groups)
    answer: dict[str, Any] = {
        "context_id": context.get("context_id"),
        "as_of": context.get("as_of"),
        "evidence": evidence,
        "groups": holds,
        "evidence_index": context_view.evidence_index(context, groups),
    }
    parsed = context_view.parse_focus(focus)
    if parsed is not None:
        answer["evidence"], answer["focused"] = context_view.focus_slice(
            context, dict(evidence), parsed
        )
    return answer


ANSWER_INSTRUCTIONS: tuple[str, ...] = (
    "You are the coach. Answer the athlete in their own language, as a coach would.",
    "Every number you state must come from the material below. If something you would "
    "want is not here, say what you do not know rather than estimating it.",
    "Lead with what to do or what happened, then the short why.",
    "Do not describe this exercise, the material's shape, or how it was delivered.",
)

DECLARE_INSTRUCTIONS: tuple[str, ...] = (
    "Before the evidence is handed to you, say what this turn is reading for.",
    "Reply with one JSON object and nothing else: "
    '{"read": ["<group>", ...]} using only these groups: '
    + ", ".join(context_view.ALL_GROUPS)
    + ".",
    "Naming every group costs more than choosing; naming too few costs one extra call.",
)

EXPAND_INSTRUCTIONS: tuple[str, ...] = (
    "You may read more of the same snapshot before answering.",
    'To read more, reply with one JSON object and nothing else: {"read": ["<group>", ...]}'
    ', optionally with "focus": {"sessions": [...]} or {"dates": [...]} or '
    '{"movements": [...]} to get one session, day or movement whole instead of whole '
    "groups.",
    "To answer now, write the answer instead. Do not do both.",
)


def reference_packet(turn: dict[str, Any], response: dict[str, Any]) -> dict[str, Any]:
    return {
        "athlete_says": turn["question"],
        "instructions": list(ANSWER_INSTRUCTIONS),
        "coaching_guidance": response.get("coaching_guidance"),
        "start_coach_session": {
            key: value
            for key, value in response.items()
            if key != "coaching_guidance"
        },
    }


def candidate_declare_packet(turn: dict[str, Any]) -> dict[str, Any]:
    return {
        "athlete_says": turn["question"],
        "instructions": list(DECLARE_INSTRUCTIONS),
        "groups": {
            name: list(fields) for name, fields in context_view.EVIDENCE_GROUPS.items()
        },
    }


def candidate_read_packet(
    turn: dict[str, Any], response: dict[str, Any], read: list[str]
) -> dict[str, Any]:
    projected = project(response, read)
    return {
        "athlete_says": turn["question"],
        "instructions": list(ANSWER_INSTRUCTIONS) + list(EXPAND_INSTRUCTIONS),
        "coaching_guidance": projected.get("coaching_guidance"),
        "start_coach_session": {
            key: value
            for key, value in projected.items()
            if key != "coaching_guidance"
        },
    }
