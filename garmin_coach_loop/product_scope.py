"""What each coach product reads, accepts and hands the model.

``product_identity`` decides which product a deployment is and keeps their state apart.
This module decides what the training product is: the coach a directory listing points
at, which never reads or keeps the categories a directory review named as health
information -- sleep, HRV, resting heart rate, body measurements and reported symptoms.
Exercise heart rate, pace, load and how the athlete says they feel stay: they are
training evidence, and the hybrid product reads them the same way.

The exclusion is made at every layer that could otherwise reach the data, because a field
missing from a schema is only a suggestion to a client:

- Intervals is never asked for ``WELLNESS:READ`` and the wellness endpoint is never
  requested -- not read and then dropped.
- The catalogue omits the tool, the input fields and the reason codes that carry them,
  and ``refusal`` refuses them on a direct call as well.
- An upload keeps its sessions and counts its body and recovery records as not kept.
- The instructions and coaching guidance the model is handed do not ask for them.

The hybrid product's served bytes are the reviewed ones and are not touched. The training
surface is derived from them by named edits, each required to match the hybrid text
exactly once: when that text changes, building the training surface fails at startup
rather than serving a sentence nobody re-read.
"""

from __future__ import annotations

import copy
import dataclasses
import functools
from collections.abc import Callable
from typing import Any

from . import mcp_transport, orchestration
from .product_identity import DEFAULT_PRODUCT, validate_product


TRAINING_PRODUCT = "training"


class ProductScopeError(RuntimeError):
    """The training surface no longer matches the hybrid text it is derived from."""


# --------------------------------------------------------------------------------------
# What the training product never reaches
# --------------------------------------------------------------------------------------

# Intervals grants each scope as its own consent box, so a scope that is never requested
# is a box the athlete is never shown.
_WITHHELD_SCOPES = frozenset({"WELLNESS:READ"})

_EXCLUDED_TOOLS = frozenset({"recordBodyMeasurement"})
_EXCLUDED_KINDS = frozenset({"body_measurement_record"})
# Route kind -> top-level request fields that carry an excluded category.
_EXCLUDED_FIELDS: dict[str, tuple[str, ...]] = {
    "session": ("red_flags", "all_clear", "recovery_signals"),
    "decision_prepare": ("red_flags",),
}
_EXCLUDED_REASON_CODES = frozenset(
    {"multi_signal_recovery_down", "recovery_signal_mixed", "pain_or_illness_flag"}
)
_EXCLUDED_RETRACT_KINDS = frozenset({"body_measurement", "recovery_reading"})

OUTSIDE_SCOPE_ERROR = "outside_product_scope"
_NOT_COLLECTED = (
    "This coach does not collect sleep, HRV, resting heart rate, body measurements or "
    "symptoms."
)
_SYMPTOM_BOUNDARY = (
    "Pain, illness, chest pain, dizziness or unusual symptoms need a human decision "
    "before training; they are not recorded here."
)

def is_training(product: str) -> bool:
    return validate_product(product) == TRAINING_PRODUCT


def oauth_scopes(product: str, scopes: tuple[str, ...]) -> tuple[str, ...]:
    """The Intervals scopes this product asks for, in the hybrid order."""
    if not is_training(product):
        return scopes
    return tuple(scope for scope in scopes if scope not in _WITHHELD_SCOPES)


def reads_recovery(product: str) -> bool:
    """Whether this product reads wellness, recovery readings and body measurements."""
    return not is_training(product)


def refusal(product: str, kind: str, body: dict[str, Any]) -> str | None:
    """Why this product will not accept one call, or ``None`` when it will.

    Checked by the gateway before any handler runs, so a client holding a cached hybrid
    catalogue, or calling the route directly, reaches the same boundary the training
    catalogue describes. A null field is an omitted one; anything else is a statement.
    """
    if not is_training(product):
        return None
    if kind in _EXCLUDED_KINDS:
        return f"{_NOT_COLLECTED} Nothing was stored."
    present = [name for name in _EXCLUDED_FIELDS.get(kind, ()) if body.get(name) is not None]
    if present:
        named = ", ".join(present)
        boundary = f" {_SYMPTOM_BOUNDARY}" if {"red_flags", "all_clear"} & set(present) else ""
        return f"{named} is not accepted. {_NOT_COLLECTED}{boundary}"
    if kind == "decision_prepare":
        change = body.get("change_request")
        codes = change.get("reason_codes") if isinstance(change, dict) else None
        if isinstance(codes, list):
            excluded = sorted(str(code) for code in codes if code in _EXCLUDED_REASON_CODES)
            if excluded:
                return (
                    f"reason_codes {', '.join(excluded)} are not accepted. {_NOT_COLLECTED}"
                )
    if kind == "athlete_record_retract" and body.get("kind") in _EXCLUDED_RETRACT_KINDS:
        return f"kind {body.get('kind')} is not a record this coach holds. {_NOT_COLLECTED}"
    return None


# What the model is not shown of a training context. The held context keeps the
# contract's required recovery keys -- unknown, empty or null, since nothing filled them --
# because validation and the decision record are shared code; an empty sleep row is still
# an invitation to ask about sleep, so the model is handed the context without them.
_HIDDEN_CONTEXT_FIELDS = (
    "coverage",
    "recovery_trends",
    "recovery_signals",
    "reported_recovery",
    "body_measurements",
)
_HIDDEN_UNKNOWNS = frozenset(
    {
        "red_flags_not_confirmed",
        "sleep_data_unavailable",
        "hrv_data_unavailable",
        "resting_hr_unavailable",
    }
)


def visible_context(product: str, context: dict[str, Any]) -> dict[str, Any]:
    """The part of one validated context this product shows the model."""
    if not is_training(product):
        return context
    shown = {key: value for key, value in context.items() if key not in _HIDDEN_CONTEXT_FIELDS}
    for key, hidden in (("freshness", "recovery"), ("constraints", "red_flags")):
        if isinstance(shown.get(key), dict):
            shown[key] = {name: value for name, value in shown[key].items() if name != hidden}
    if isinstance(shown.get("unknowns"), list):
        shown["unknowns"] = [entry for entry in shown["unknowns"] if entry not in _HIDDEN_UNKNOWNS]
    return shown


def state_read_unknowns(product: str, unknowns: tuple[str, ...]) -> tuple[str, ...]:
    """``getCoachState``'s standing unknowns, without asking for symptoms it cannot take."""
    if not is_training(product):
        return unknowns
    edited = tuple(
        entry.replace("(red flags, soreness, availability)", "(soreness, availability)")
        for entry in unknowns
    )
    if edited == unknowns:
        raise ProductScopeError("getCoachState unknowns no longer name red flags")
    return edited


# --------------------------------------------------------------------------------------
# Deriving the training surface from the hybrid one
# --------------------------------------------------------------------------------------


def _once(text: str, old: str, new: str) -> str:
    if text.count(old) != 1:
        raise ProductScopeError(
            f"training edit no longer matches the hybrid text exactly once: {old[:80]!r}"
        )
    return text.replace(old, new)


def _drop(mapping: dict[str, Any], *names: str) -> None:
    for name in names:
        if name not in mapping:
            raise ProductScopeError(f"training edit names a field the hybrid lacks: {name}")
        del mapping[name]


def _without(values: list[Any], excluded: frozenset[str]) -> list[Any]:
    if not excluded <= set(values):
        raise ProductScopeError(f"training edit names values the hybrid lacks: {sorted(excluded)}")
    return [value for value in values if value not in excluded]


def _session(tool: mcp_transport.Tool) -> mcp_transport.Tool:
    schema = copy.deepcopy(tool.input_schema)
    properties = schema["properties"]
    _drop(properties, *_EXCLUDED_FIELDS["session"])
    properties["read"]["description"] = _once(
        properties["read"]["description"],
        "recovery: sleep, HRV, resting heart rate. ",
        "recovery: how the athlete said they felt. ",
    )
    output = copy.deepcopy(tool.output_schema)
    _drop(output["properties"], "recovery_recording")
    # The hybrid tool is destructive only because a recovery upload overwrites a stated
    # reading; without one, reconciliation is the append-only plan chain it always was.
    annotations = {**tool.annotations, "destructiveHint": False}
    return dataclasses.replace(
        tool, input_schema=schema, output_schema=output, annotations=annotations
    )


def _prepare_decision(tool: mcp_transport.Tool) -> mcp_transport.Tool:
    schema = copy.deepcopy(tool.input_schema)
    properties = schema["properties"]
    _drop(properties, *_EXCLUDED_FIELDS["decision_prepare"])
    change = properties["change_request"]["properties"]
    codes = change["reason_codes"]["items"]
    codes["enum"] = _without(codes["enum"], _EXCLUDED_REASON_CODES)
    field = change["evidence"]["items"]["properties"]["field"]
    field["description"] = _once(
        field["description"], "recovery_trends.hrv or recent_actuals", "recent_actuals"
    )
    return dataclasses.replace(tool, input_schema=schema)


def _subjective_state(tool: mcp_transport.Tool) -> mcp_transport.Tool:
    text = _once(tool.description, "我覺得很累, 最近睡不好, 腿還是很沉", "我覺得很累, 腿還是很沉")
    text = _once(text, "not scored, not turned into a recovery number, and", "not scored, and")
    text = _once(
        text,
        "Symptoms do not come here: pain, illness, chest pain, dizziness or unusual "
        "symptoms are startCoachSession's red_flags, which limit the day rather than "
        "wait to be read.",
        f"Symptoms do not come here. {_SYMPTOM_BOUNDARY}",
    )
    return dataclasses.replace(tool, description=text)


def _retract(tool: mcp_transport.Tool) -> mcp_transport.Tool:
    text = _once(
        tool.description,
        "re-send the matching record tool, or startCoachSession's recovery_signals for a "
        "recovery correction. recovery_reading removes all stored self-reported readings "
        "for the date and leaves provider readings untouched; session_not_trained",
        "re-send the matching record tool. session_not_trained",
    )
    schema = copy.deepcopy(tool.input_schema)
    kind = schema["properties"]["kind"]
    kind["enum"] = _without(kind["enum"], _EXCLUDED_RETRACT_KINDS)
    return dataclasses.replace(tool, description=text, input_schema=schema)


def _history_import(tool: mcp_transport.Tool) -> mcp_transport.Tool:
    schema = copy.deepcopy(tool.input_schema)
    content = schema["properties"]["content"]
    content["description"] = _once(
        content["description"],
        "the <Workout> and body-mass <Record> elements",
        "the <Workout> elements",
    )
    output = copy.deepcopy(tool.output_schema)
    _drop(
        output["properties"],
        "measurements_added",
        "measurements_skipped",
        "recovery_added",
        "recovery_skipped",
    )
    return dataclasses.replace(tool, input_schema=schema, output_schema=output)


def _apply_decision(tool: mcp_transport.Tool) -> mcp_transport.Tool:
    text = _once(
        tool.description,
        "Report new symptoms through startCoachSession before applying.",
        "Do not apply after the athlete reports a new symptom; it needs a human decision "
        "first.",
    )
    return dataclasses.replace(tool, description=text)


def _long_term_goal(tool: mcp_transport.Tool) -> mcp_transport.Tool:
    # A weight goal is still the athlete's to state; it is just not an example this
    # coach offers, since it keeps no body measurement to read progress against.
    text = _once(tool.description, "-- a body weight, a VO2max,", "-- a VO2max,")
    schema = copy.deepcopy(tool.input_schema)
    metric = schema["properties"]["metric"]
    metric["description"] = _once(metric["description"], "體重, VO2max, 5K", "VO2max, 5K, 深蹲")
    return dataclasses.replace(tool, description=text, input_schema=schema)


_TOOL_EDITS: dict[str, Callable[[mcp_transport.Tool], mcp_transport.Tool]] = {
    "startCoachSession": _session,
    "prepareCoachDecision": _prepare_decision,
    "applyCoachDecision": _apply_decision,
    "recordLongTermGoal": _long_term_goal,
    "recordSubjectiveState": _subjective_state,
    "retractAthleteRecord": _retract,
    "importAthleteHistory": _history_import,
}


def training_tools(tools: tuple[mcp_transport.Tool, ...]) -> tuple[mcp_transport.Tool, ...]:
    """The training catalogue: the hybrid tools, less the excluded ones, edited."""
    names = {tool.name for tool in tools}
    missing = (_EXCLUDED_TOOLS | set(_TOOL_EDITS)) - names
    if missing:
        raise ProductScopeError(f"training edits name tools the hybrid lacks: {sorted(missing)}")
    return tuple(
        _TOOL_EDITS.get(tool.name, lambda unchanged: unchanged)(tool)
        for tool in tools
        if tool.name not in _EXCLUDED_TOOLS
    )


def training_instructions(text: str) -> str:
    """The orchestration prompt without the routes this product does not have."""
    text = _once(
        text,
        "- A stated weight or body fat goes to `recordBodyMeasurement`; a session no device\n"
        "  recorded to `recordActivitySummary`; an uploaded export -- CSV, Apple Health, `.fit`\n"
        "  -- to `importAthleteHistory`.",
        "- A session no device recorded goes to `recordActivitySummary`; an uploaded export\n"
        "  -- CSV, Apple Health, `.fit` -- to `importAthleteHistory`.",
    )
    return _once(
        text,
        "- How they feel: `recordSubjectiveState`, in their words; symptoms to\n"
        "  `startCoachSession.red_flags` before apply. Nothing fires on a stored note.",
        "- How they feel: `recordSubjectiveState`, in their words. Nothing fires on a stored\n"
        "  note. This coach takes no sleep, HRV, resting heart rate, body measurement or\n"
        "  symptom; never ask for or record one.",
    )


_BODY_COMPOSITION_HEADING = "\n\n## Body composition is a lens, not a target\n"


def training_judgment(text: str) -> str:
    """The training guidance without the readings this product does not take."""
    head, found, tail = text.partition(_BODY_COMPOSITION_HEADING)
    if not found or "\n## " in tail or _BODY_COMPOSITION_HEADING in tail:
        raise ProductScopeError("body composition is no longer the last guidance section")
    text = _once(
        head,
        "read it\nagainst that day's recovery state. Where the recovery reading is "
        "unavailable, that\nis unknown",
        "read it\nagainst what the athlete said about how they felt that day. Where they said\n"
        "nothing, that is unknown",
    )
    text = _once(
        text,
        "Use multi-signal recovery trends in context. One wearable value should not flip\n"
        "the plan, while a current red flag remains important regardless of wearable\n"
        "scores. Respect",
        "Respect",
    )
    return _once(
        text,
        "such as sleep,\navailable time, or nutrition",
        "such as\navailable time or nutrition",
    )


# --------------------------------------------------------------------------------------
# One surface per product
# --------------------------------------------------------------------------------------


def _prompt_messages(description: str, text: str) -> dict[str, Any]:
    return {
        "description": description,
        "messages": [{"role": "user", "content": {"type": "text", "text": text}}],
    }


@dataclasses.dataclass(frozen=True)
class Surface:
    """Everything a client of one product is handed: tools, prompts and instructions."""

    product: str
    tools: tuple[mcp_transport.Tool, ...]
    instructions: Callable[[], str]
    training_judgment: Callable[[], str]

    @functools.cached_property
    def tools_by_name(self) -> dict[str, mcp_transport.Tool]:
        return {tool.name: tool for tool in self.tools}

    @functools.cached_property
    def prompts(self) -> dict[str, tuple[Callable[[], Any], Callable[[], Any]]]:
        if not is_training(self.product):
            return orchestration.PROMPTS
        return {
            orchestration.PROMPT_NAME: (
                orchestration.prompt_descriptor,
                lambda: _prompt_messages(orchestration.PROMPT_DESCRIPTION, self.instructions()),
            ),
            orchestration.TRAINING_PROMPT_NAME: (
                orchestration.training_prompt_descriptor,
                lambda: _prompt_messages(
                    orchestration.TRAINING_PROMPT_DESCRIPTION, self.training_judgment()
                ),
            ),
        }

    def tool_catalogue_sha256(self) -> str:
        return mcp_transport.tool_catalogue_sha256(self.tools)


# Looked up at call time rather than bound here, so the hybrid surface is exactly
# whatever `orchestration` answers -- the module the release binds and the tests patch.
HYBRID_SURFACE = Surface(
    product=DEFAULT_PRODUCT,
    tools=mcp_transport.TOOLS,
    instructions=lambda: orchestration.instructions(),
    training_judgment=lambda: orchestration.training_judgment(),
)


@functools.cache
def _training_surface() -> Surface:
    surface = Surface(
        product=TRAINING_PRODUCT,
        tools=training_tools(mcp_transport.TOOLS),
        instructions=lambda: training_instructions(orchestration.instructions()),
        training_judgment=lambda: training_judgment(orchestration.training_judgment()),
    )
    # Derived once here so a hybrid text change fails the deployment's startup, not the
    # first athlete's turn.
    surface.instructions()
    surface.training_judgment()
    return surface


def surface(product: str) -> Surface:
    """The surface one deployment serves; built eagerly so a stale edit refuses boot."""
    return _training_surface() if is_training(product) else HYBRID_SURFACE
