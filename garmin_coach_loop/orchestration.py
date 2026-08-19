"""The two layers every entry hands the model, each read from one file.

A model given the tool catalogue alone knows what each operation *takes*. It does not
know which one to call first, that exactly one confirmation stands between a preview and
a write, or that Intervals accepting a workout says nothing about the watch. That layer
is product-specific and cannot be inferred from schemas, so every entry has to supply it
-- and for a long time only one did. It was pasted, by hand, into one client's console;
an MCP client received `tools/list` and nothing above it (issue #125).

So the text moved here, into the package the gateway is deployed as, and there is exactly
one copy of it:

- **The MCP entry serves it** as a prompt, synced to the client at connect time next to
  the tools it describes (``mcp_transport``).
- **The release binds it** -- `scripts/release_bundle.py` reads this same file at the
  released commit and binds its digest into the release identity, so a deploy that
  shipped new code against an old prompt is visible at ``/readyz``.

Keeping it as Markdown rather than a Python string literal is what makes the second one
work: the release path reads the file out of Git at a specific commit, which stays
readable and diffable that way. Keeping it *inside* the package is what makes the first
one work: the deployment image copies ``garmin_coach_loop/`` and nothing else.

What belongs in each is bounded by AGENTS.md 11 and by issue #82's boundary, and the two
do not mix. ``orchestration.md`` owns product-specific orchestration only: what the source
of truth is, which boundary needs an explicit confirmation, and what the product may claim
to have observed. ``hybrid_training.md`` owns the training judgment: cycle direction, week
arrangement, anchors, progression, evidence quality. A rule that would change what the
coach *decides* goes in the second and never in the first. Field semantics belong to
neither -- they are in the tool schemas and ``contracts/``.

The training layer used to be the Skill's alone, so a client that reached this product
over MCP got the sequencing and not the coaching. That made the hosted entry a thinner
coach than the local one for no reason a reader of either could see, so it is served here
too. It had to move into the package to be served at all: the deployment image copies
``garmin_coach_loop/`` and nothing else.

Both have a budget, and the budget is the point rather than the number. Every MCP client
is handed these files at connect time and carries them for the whole conversation, so a
paragraph here is a paragraph of every future turn's context. The orchestration ceiling
started as one client's paste limit; it stays because unbounded growth there is how an
orchestration layer becomes a shadow coach one reasonable-sounding sentence at a time. A
new paragraph therefore costs an old one (tests/test_orchestration_prompt.py holds both).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any


PROMPT_NAME = "coach_orchestration"
PROMPT_TITLE = "Coach orchestration"
PROMPT_DESCRIPTION = (
    "How to drive the coach operations: which call answers a question, where exactly "
    "one explicit confirmation stands before a write, and what a delivery result may "
    "be said to prove. Fetch this before the first coaching turn."
)

_SOURCE = Path(__file__).with_name("orchestration.md")


def instructions() -> str:
    """The orchestration text, exactly as a client receives it.

    Trailing newlines are stripped, and stay stripped now that the console which stripped
    them on save is no longer a supported entry: this is the value ``prompts/get`` serves
    and the value whose digest is bound into the release identity, and moving it would
    change both for no gain (``scripts/release_bundle.py`` does the same).
    """
    return _SOURCE.read_text(encoding="utf-8").rstrip("\r\n")


def prompt_descriptor() -> dict[str, Any]:
    """This prompt as ``prompts/list`` states it -- no arguments, so none are declared."""
    return {
        "name": PROMPT_NAME,
        "title": PROMPT_TITLE,
        "description": PROMPT_DESCRIPTION,
    }


def prompt_messages() -> dict[str, Any]:
    """This prompt as ``prompts/get`` returns it.

    One user message, because that is the role a client can put in front of its own model
    without claiming the model already said it.
    """
    return {
        "description": PROMPT_DESCRIPTION,
        "messages": [
            {"role": "user", "content": {"type": "text", "text": instructions()}}
        ],
    }


# --------------------------------------------------------------------------------------
# The training judgment
#
# Deliberately a second prompt rather than more of the first. A client may hold the
# orchestration layer and its own coaching, and the two answer different questions: one
# is how to drive this product, the other is how to coach. Keeping them apart is also
# what keeps the orchestration budget honest -- a coaching sentence has somewhere else to
# go, so it never has to be argued into the file that must not become a shadow coach.
# --------------------------------------------------------------------------------------

TRAINING_PROMPT_NAME = "coach_training_judgment"
TRAINING_PROMPT_TITLE = "Hybrid training judgment"
TRAINING_PROMPT_DESCRIPTION = (
    "How to coach the hybrid week itself: which cycle direction the evidence supports, "
    "how to arrange running beside strength, which anchors a prescription may use, and "
    "what execution evidence does and does not prove. Fetch this beside the "
    "orchestration prompt; neither contains the other."
)

_TRAINING_SOURCE = Path(__file__).with_name("hybrid_training.md")


def training_judgment() -> str:
    """The training text, exactly as a client receives it."""
    return _TRAINING_SOURCE.read_text(encoding="utf-8").rstrip("\r\n")


def training_prompt_descriptor() -> dict[str, Any]:
    return {
        "name": TRAINING_PROMPT_NAME,
        "title": TRAINING_PROMPT_TITLE,
        "description": TRAINING_PROMPT_DESCRIPTION,
    }


def training_prompt_messages() -> dict[str, Any]:
    return {
        "description": TRAINING_PROMPT_DESCRIPTION,
        "messages": [
            {"role": "user", "content": {"type": "text", "text": training_judgment()}}
        ],
    }


# name -> (descriptor, messages). One registry, so `prompts/list` and `prompts/get`
# cannot come to disagree about what this server serves.
PROMPTS: dict[str, tuple[Any, Any]] = {
    PROMPT_NAME: (prompt_descriptor, prompt_messages),
    TRAINING_PROMPT_NAME: (training_prompt_descriptor, training_prompt_messages),
}
