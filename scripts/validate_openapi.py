#!/usr/bin/env python3
"""Full OpenAPI 3.1 validation of entrypoints/custom-gpt/openapi.yaml.

Development/CI-only tool. Requires ``openapi-spec-validator`` and ``PyYAML``,
installed by the CI workflow (see .github/workflows/ci.yml) -- never a runtime
dependency of garmin_coach_loop itself (AGENTS.md: runtime stays stdlib-only).
This module must never be imported by runtime code.

tests/test_openapi_contract.py is a line-level, fixed-indentation text scan
that keeps the documented routes honest against garmin_coach_loop.gateway.
ROUTES; by its own docstring it does not parse YAML and would not notice a
malformed schema. This script is the complementary, heavier check that a real
parse enables. Three layers, each catching what the others do not:

1. ``openapi_spec_validator.validate`` -- OpenAPI-level structural validation
   (paths, responses, duplicate operationIds, path/parameter consistency) for
   the document's declared version.
2. A full recursive ``$ref`` resolution pass over the entire parsed document.
   openapi_spec_validator's own traversal walks ``responses`` and
   ``parameters`` but never ``requestBody`` -- verified empirically against
   this library version -- so a dangling $ref under a requestBody schema
   (most POST operations in this file have one) passes it silently. This pass
   walks every dict/list in the document and resolves every "$ref" pointer
   against the root, independent of which OpenAPI keyword it sits under.
3. Meta-schema validation of every ``components.schemas`` entry with
   ``openapi_schema_validator.OAS31Validator.check_schema`` -- confirms each
   schema is itself a syntactically valid JSON Schema 2020-12 document.
   openapi_spec_validator's own component-schema walk checks only a few
   structural rules (required/properties consistency, default values); it
   does not reject an invalid keyword value such as ``type: not_a_real_type``
   -- also verified empirically.

Usage:
    python3 scripts/validate_openapi.py [path/to/openapi.yaml]

Defaults to entrypoints/custom-gpt/openapi.yaml. Prints every problem found
and exits non-zero on any failure; exits 0 only when all three layers pass.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Iterator


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OPENAPI_PATH = ROOT / "entrypoints" / "custom-gpt" / "openapi.yaml"

_INSTALL_HINT = (
    "    pip install openapi-spec-validator==0.7.2 PyYAML==6.0.3\n"
    "This is a development/CI-only tool -- never add it to runtime requirements\n"
    "(AGENTS.md: runtime stays stdlib-only)."
)

try:
    import yaml
except ImportError:
    print(
        f"PyYAML is required to run this validator. Install with:\n{_INSTALL_HINT}",
        file=sys.stderr,
    )
    raise SystemExit(2)

try:
    from openapi_spec_validator import validate as validate_openapi_spec
except ImportError:
    print(
        f"openapi-spec-validator is required to run this validator. Install with:\n{_INSTALL_HINT}",
        file=sys.stderr,
    )
    raise SystemExit(2)

from openapi_schema_validator import OAS31Validator


def _unescape_json_pointer_segment(segment: str) -> str:
    # RFC 6901 escape order matters: ~1 -> / must be decoded before ~0 -> ~.
    return segment.replace("~1", "/").replace("~0", "~")


def _resolve_json_pointer(document: Any, pointer: str) -> Any:
    """Resolve a local ``#/a/b/c`` JSON pointer against ``document``.

    Raises KeyError, IndexError, ValueError or TypeError when any segment does
    not exist -- a missing object key, an out-of-range or non-numeric array
    index, or indexing into a scalar all count as "does not resolve" to the
    caller, which catches all four.
    """
    if pointer == "#":
        return document
    assert pointer.startswith("#/"), pointer
    node = document
    for raw_segment in pointer[2:].split("/"):
        segment = _unescape_json_pointer_segment(raw_segment)
        if isinstance(node, list):
            node = node[int(segment)]
        else:
            node = node[segment]
    return node


def _iter_refs(node: Any, path: str = "$") -> Iterator[tuple[str, str]]:
    """Yield (json-path-of-occurrence, $ref value) for every $ref in the tree."""
    if isinstance(node, dict):
        ref = node.get("$ref")
        if isinstance(ref, str):
            yield path, ref
        for key, value in node.items():
            if key == "$ref":
                continue
            yield from _iter_refs(value, f"{path}.{key}")
    elif isinstance(node, list):
        for index, item in enumerate(node):
            yield from _iter_refs(item, f"{path}[{index}]")


def _check_all_refs_resolve(document: dict) -> list[str]:
    """Every $ref anywhere in the document, resolved against the same document.

    This document never references external files (verified: every $ref in it
    starts with "#/components/schemas/"), so a non-local $ref is itself a
    problem worth reporting rather than silently skipping.
    """
    problems = []
    for occurrence_path, ref in _iter_refs(document):
        if not ref.startswith("#/") and ref != "#":
            problems.append(
                f"{occurrence_path}: $ref {ref!r} is not a local '#/...' pointer; "
                "this document must not reference external files"
            )
            continue
        try:
            _resolve_json_pointer(document, ref)
        except (KeyError, IndexError, TypeError, ValueError):
            problems.append(f"{occurrence_path}: $ref {ref!r} does not resolve")
    return problems


def _truncate(message: object, limit: int = 400) -> str:
    """Keep a validator exception's message CI-log-sized.

    Some upstream exceptions (referencing.exceptions.PointerToNowhere in
    particular) embed the *entire* resolved document in their message, which
    is accurate but unreadable at document size. The full detail is never
    lost: _check_all_refs_resolve reports the same broken $ref with its exact
    location on its own, short, line.
    """
    text = str(message)
    if len(text) <= limit:
        return text
    return f"{text[:limit]}... (truncated; see the matching $ref line below, if any)"


def _check_component_schemas_are_valid_json_schema(document: dict) -> list[str]:
    problems = []
    components = document.get("components")
    if not isinstance(components, dict):
        return problems
    schemas = components.get("schemas")
    if not isinstance(schemas, dict):
        return problems
    for name, schema in schemas.items():
        try:
            OAS31Validator.check_schema(schema)
        except Exception as exc:  # jsonschema.exceptions.SchemaError, broadly
            problems.append(f"components.schemas.{name}: {exc}")
    return problems


def validate_document(path: Path) -> list[str]:
    """Return every problem found; an empty list means the document is valid."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        return [f"cannot read {path}: {exc}"]

    try:
        document = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        return [f"invalid YAML: {exc}"]

    if not isinstance(document, dict):
        return ["document does not parse to a YAML mapping at the top level"]

    problems: list[str] = []

    try:
        validate_openapi_spec(document, base_uri=path.as_uri())
    except Exception as exc:
        # Broad on purpose: openapi_spec_validator raises jsonschema
        # ValidationError subclasses for structural problems, but a broken
        # $ref it does happen to walk raises referencing.exceptions.
        # PointerToNowhere, an unrelated exception hierarchy. Every one of
        # them is a real validation failure this script must report.
        problems.append(
            f"OpenAPI structural validation: {type(exc).__name__}: {_truncate(exc)}"
        )

    problems.extend(_check_all_refs_resolve(document))
    problems.extend(_check_component_schemas_are_valid_json_schema(document))
    return problems


def main(argv: list[str]) -> int:
    target = Path(argv[0]).resolve() if argv else DEFAULT_OPENAPI_PATH
    problems = validate_document(target)
    if problems:
        print(f"OpenAPI validation failed for {target}:")
        for problem in problems:
            print(f"- {problem}")
        return 1
    print(f"OpenAPI validation passed for {target}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
