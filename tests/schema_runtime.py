"""Test-only helper: published-schema field lists and prepare→apply identifier copy.

The field list a conformance test walks must come from the catalogue a client is
handed, not from a second handwritten copy of it. A handwritten list is the
self-consistency trap issue #427 named: the suite encodes the author's view of
the contract and cannot disagree with it.

This module is imported only by tests. It does not change product behaviour.
"""

from __future__ import annotations

from typing import Any, Iterable

from garmin_coach_loop.mcp_transport import TOOLS_BY_NAME


IMPORTANT_PUBLIC_TOOLS = (
    "startCoachSession",
    "readCoachEvidence",
    "prepareCoachDecision",
    "applyCoachDecision",
    "prepareWorkoutDelivery",
    "applyWorkoutDelivery",
)

PREPARE_APPLY_PAIRS = (
    ("prepareCoachDecision", "applyCoachDecision"),
    ("prepareWorkoutDelivery", "applyWorkoutDelivery"),
)

LOAD_BEARING = "load-bearing"
ACCEPTED_IGNORED = "accepted-and-ignored"
REJECTED = "rejected"
DISPOSITIONS = {LOAD_BEARING, ACCEPTED_IGNORED, REJECTED}


def input_schema(tool_name: str) -> dict[str, Any]:
    return TOOLS_BY_NAME[tool_name].input_schema


def output_schema(tool_name: str) -> dict[str, Any]:
    return TOOLS_BY_NAME[tool_name].output_schema


def path_label(path: tuple[Any, ...]) -> str:
    if not path:
        return "$"
    parts: list[str] = []
    for step in path:
        if step == "[]":
            if parts:
                parts[-1] = parts[-1] + "[]"
            else:
                parts.append("[]")
        else:
            parts.append(str(step))
    return ".".join(parts)


def _one_of_label(key: str, index: int, branch: dict[str, Any]) -> str:
    kind = ((branch.get("properties") or {}).get("kind") or {}).get("enum") or []
    if len(kind) == 1:
        return f"{key}:{kind[0]}"
    return f"{key}:{index}"


def property_paths(
    schema: dict[str, Any] | None, prefix: tuple[Any, ...] = ()
) -> tuple[tuple[Any, ...], ...]:
    """Every property path the published schema declares, including nested objects.

    `[]` steps into array items. `oneOf:<kind>` / `anyOf:<kind>` selects a unique
    branch by its `kind` enum when the branch has one; otherwise the branch index
    is used. `additionalProperties: true` objects with no listed properties are
    leaves -- their extra keys are not a published field list.
    """
    found: list[tuple[Any, ...]] = []
    if not isinstance(schema, dict):
        return tuple(found)
    for name, sub in (schema.get("properties") or {}).items():
        path = prefix + (name,)
        found.append(path)
        found.extend(property_paths(sub, path))
    items = schema.get("items")
    if isinstance(items, dict):
        found.extend(property_paths(items, prefix + ("[]",)))
    for key in ("oneOf", "anyOf"):
        for index, branch in enumerate(schema.get(key) or []):
            if not isinstance(branch, dict):
                continue
            found.extend(
                property_paths(branch, prefix + (_one_of_label(key, index, branch),))
            )
    return tuple(found)


def schema_node(schema: dict[str, Any], path: tuple[Any, ...]) -> dict[str, Any]:
    """The schema object at this path. `[]` is array items; `oneOf:<kind>` a branch."""
    node: Any = schema
    for step in path:
        if step == "[]":
            node = node["items"]
            continue
        if isinstance(step, str) and (
            step.startswith("oneOf:") or step.startswith("anyOf:")
        ):
            key, label = step.split(":", 1)
            branches = node.get(key) or []
            matched = []
            for index, branch in enumerate(branches):
                if not isinstance(branch, dict):
                    continue
                if _one_of_label(key, index, branch).split(":", 1)[1] == label:
                    matched.append(branch)
            if len(matched) != 1:
                raise AssertionError(f"no unique {key} branch {label!r} at {path!r}")
            node = matched[0]
            continue
        node = node["properties"][step]
    return node


def published_fields(tool_name: str) -> tuple[tuple[Any, ...], ...]:
    return property_paths(input_schema(tool_name))


def is_handoff_identifier(name: str) -> bool:
    """The *rule* for which prepare-output property names are identifiers.

    Not a list of the current names: a new `*_id` or `*_version` on the prepare
    output schema is an identifier the next matching apply might be handed.
    """
    return name == "proposal" or name.endswith("_id") or name.endswith("_version")


def handoff_identifier_names(prepare_tool: str) -> tuple[str, ...]:
    properties = output_schema(prepare_tool).get("properties") or {}
    return tuple(name for name in properties if is_handoff_identifier(name))


def returned_handoff_identifiers(
    prepared: dict[str, Any], prepare_tool: str
) -> dict[str, Any]:
    """Identifiers the actual prepare result returned, never synthesized."""
    returned: dict[str, Any] = {}
    for name in handoff_identifier_names(prepare_tool):
        if name in prepared and prepared[name] is not None:
            returned[name] = prepared[name]
    return returned


def apply_body_from_prepare(
    prepared: dict[str, Any],
    apply_tool: str,
    *,
    confirmed: bool = True,
) -> dict[str, Any]:
    """Build an apply body the way a schema-reading model would.

    Copy every apply-input property that the prepare result actually returned.
    `confirmed` is the athlete's yes, supplied by the caller, never invented from
    the preview. Missing required apply keys other than `confirmed` fail rather
    than being filled in from the consumer schema.
    """
    schema = input_schema(apply_tool)
    properties = schema.get("properties") or {}
    required = set(schema.get("required") or [])
    body: dict[str, Any] = {}
    missing: list[str] = []
    for name in properties:
        if name == "confirmed":
            body["confirmed"] = confirmed
            continue
        if name in prepared and prepared[name] is not None:
            body[name] = prepared[name]
        elif name in required:
            missing.append(name)
    if missing:
        raise AssertionError(
            f"{apply_tool} requires {missing} but the matching prepare result did "
            "not return them; refusing to invent identifiers"
        )
    if not body.get("proposal"):
        raise AssertionError(
            "producer did not return proposal; refusing to synthesize it"
        )
    return body


def identifiers_apply_schema_rejects(
    prepared: dict[str, Any], prepare_tool: str, apply_tool: str
) -> dict[str, Any]:
    """Returned identifiers that are not apply-input properties.

    Apply's published schema is `additionalProperties: false` and the runtime
    `_require_apply_fields` refuses anything else. A prepare result that hands
    one of these back is a self-contradictory handoff: the next matching call
    rejects a value the previous call just emitted (issue #280).
    """
    apply_properties = set((input_schema(apply_tool).get("properties") or {}))
    returned = returned_handoff_identifiers(prepared, prepare_tool)
    return {
        name: value
        for name, value in returned.items()
        if name not in apply_properties
    }


def coverage_problems(
    *,
    declared: Iterable[tuple[Any, ...]],
    runtime_known: Iterable[tuple[Any, ...]],
    dispositions: dict[tuple[Any, ...], str],
) -> list[str]:
    """The two #427 failure classes, plus an unclassified advertised field.

    A field the schema advertises must have a recorded disposition and, unless
    it is rejected, must be in the runtime's known set. A field the runtime
    knows that the schema does not declare is the other direction of drift.
    Silent drop is: advertised, not rejected, not in the runtime known set.
    """
    declared_set = set(declared)
    runtime_set = set(runtime_known)
    problems: list[str] = []
    for path in sorted(declared_set, key=path_label):
        if path not in dispositions:
            problems.append(
                f"schema declares {path_label(path)} with no recorded disposition"
            )
            continue
        disposition = dispositions[path]
        if disposition not in DISPOSITIONS:
            problems.append(
                f"{path_label(path)} disposition {disposition!r} is not "
                f"{sorted(DISPOSITIONS)}"
            )
            continue
        if path not in runtime_set:
            problems.append(
                f"schema declares {path_label(path)} but the runtime does not "
                f"know it ({disposition}: silent drop if the call still succeeds)"
            )
    for path in sorted(runtime_set - declared_set, key=path_label):
        problems.append(
            f"runtime honours {path_label(path)} which the published schema "
            "does not declare"
        )
    return problems
