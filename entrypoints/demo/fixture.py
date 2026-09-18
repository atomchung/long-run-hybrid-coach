"""The one athlete this demo ever reads, and the canonical readers it is read through.

The demo does not connect to Intervals, so there is no account to build a context from.
What stands in its place is a committed pair of files -- a CoachContext and the PlanState
beside it -- written to the product's own contracts and held to the product's own
validators by ``tests/test_demo_fixture.py``. That is the whole of the substitution: every
reader below is the one the hosted gateway uses, pointed at a fixture instead of at a
provider read.

Deliberately synthetic, and deliberately incomplete. A fixture where every field is
populated would teach the demo to answer as though evidence is always there, which is the
one habit this product spends the most code refusing (AGENTS.md 3). So the athlete has a
strength session nobody logged, four nights of HRV out of seven, two comparable threshold
sessions with no per-segment heart rate, and a declared measurement that has not been run.
Those gaps are listed in ``fixtures/manifest.json`` and named in the context's own
``unknowns``; a reply that reads any of them as a zero is a wrong reply, not a terse one.
"""

from __future__ import annotations

import copy
import json
import functools
from pathlib import Path
from typing import Any

from garmin_coach_loop import context_view, validation


FIXTURE_DIR = Path(__file__).with_name("fixtures")

CONTEXT_PATH = FIXTURE_DIR / "coach-context.json"
PLAN_PATH = FIXTURE_DIR / "plan-state.json"
MANIFEST_PATH = FIXTURE_DIR / "manifest.json"
ACCEPTANCE_PATH = FIXTURE_DIR / "acceptance-prompts.json"

# The fixture is written in English and its prescriptions are rendered in it, so a
# plan-change preview has to render in the same language or the week comes back written
# two ways. Not a demo setting: `garmin_coach_loop.prescription` owns both spellings.
LANGUAGE = "en"


def _read(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


@functools.lru_cache(maxsize=None)
def _cached(path: Path) -> Any:
    return _read(path)


def context() -> dict[str, Any]:
    """The whole CoachContext, as its own copy.

    A copy every time on purpose. One process serves every demo session, and a fixture
    handed out by reference is one bug away from a session editing the athlete every other
    session is reading.
    """
    return copy.deepcopy(_cached(CONTEXT_PATH))


def plan_state() -> dict[str, Any]:
    """The whole PlanState, as its own copy. Same reason."""
    return copy.deepcopy(_cached(PLAN_PATH))


def manifest() -> dict[str, Any]:
    return copy.deepcopy(_cached(MANIFEST_PATH))


def acceptance_prompts() -> list[dict[str, Any]]:
    return copy.deepcopy(_cached(ACCEPTANCE_PATH))["prompts"]


def acceptance_conversation() -> dict[str, Any]:
    """The three turns that are only answerable in order, in one conversation.

    Committed beside the separate turns rather than derived from them, because what it
    tests is the one thing they cannot: each of the last two names something said in an
    earlier turn, so a conversation that lost its history answers them by asking which
    option was meant -- which is exactly the failure a visitor met in public.
    """
    return copy.deepcopy(_cached(ACCEPTANCE_PATH))["conversation"]


def validate() -> dict[str, list[str]]:
    """Both fixtures against the product's own validators, reported together.

    Called at startup as well as from the tests: a fixture that has drifted past the
    contract is a demo that answers from a shape the product no longer has, and the
    cheapest moment to find that out is before the service accepts its first request.
    """
    context_report = validation.validate_coach_context(context())
    plan_report = validation.validate_plan_state(plan_state())
    return {
        "errors": [
            *(f"context: {line}" for line in context_report["errors"]),
            *(f"plan: {line}" for line in plan_report["errors"]),
        ],
        "warnings": [
            *(f"context: {line}" for line in context_report["warnings"]),
            *(f"plan: {line}" for line in plan_report["warnings"]),
        ],
    }


def projected(read: Any = None) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """One evidence read, through ``context_view`` -- the projection the gateway serves.

    Reused rather than reimplemented so that what the demo hands a model is the same shape,
    the same field grouping and the same "what this read left out" index a connected client
    receives. ``read`` takes the same values ``startCoachSession.read`` does, including
    ``"all"``.
    """
    groups = context_view.parse_read(read)
    return context_view.project_context(context(), groups)


def evidence_slice(read: Any) -> tuple[dict[str, Any], dict[str, list[str]]]:
    """The named groups' fields, once each, with the map of which group holds what."""
    return context_view.group_slice(context(), context_view.parse_read(read))
