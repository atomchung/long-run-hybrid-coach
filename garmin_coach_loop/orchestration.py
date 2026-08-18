"""The one orchestration layer every entry hands the model, read from one file.

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

What belongs in it is bounded by AGENTS.md 11 and by issue #82's boundary. This file owns
product-specific orchestration only: what the source of truth is, which boundary needs an
explicit confirmation, and what the product may claim to have observed. Field semantics
belong to the tool schemas and ``contracts/``; training judgment belongs to the Skill's
``references/hybrid-training.md`` and is not something the server pushes at connect time.
A rule that would change what the coach *decides* does not go here.

It also has a budget, and the budget is the point rather than the number. Every MCP
client is handed this file at connect time and carries it for the whole conversation, so
a paragraph here is a paragraph of every future turn's context. The ceiling started as
one client's paste limit; it stays because unbounded growth here is how an orchestration
layer becomes a shadow coach one reasonable-sounding sentence at a time. A new paragraph
therefore costs an old one (tests/test_openapi_contract.py holds the budget).
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
