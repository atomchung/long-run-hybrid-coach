"""A property the schema declares is a promise to the model. Keep both sides to it.

`additionalProperties: false` closes the schema against fields the code does not know.
Nothing closed the code against fields the schema *declares* -- and a model only ever
sees the schema. Two fields lived on the wrong side of that gap from the first public
commit: `change_request.cycle.end` and `change_request.sessions[].coach_note` were
declared, described, and refused by the one call every new athlete has to make. Five
cold models authoring the same first plan produced the same four refusals, every time.

No existing test could notice. Every first-plan test builds on one hand-written fixture
whose keys are exactly the set the validator accepts, because it was written by reading
the validator -- so the suite encodes the validator's own view of the contract and can
never disagree with it. This module reads the other side: the schema a client is handed.

Each declared input path is classified for first-plan mode as one of:

- accepted by runtime;
- translated / server-derived and consistency-checked;
- contextually forbidden, with a documented reason;
- opaque passthrough, where that is deliberately the contract.

The same key may mean different things at different paths, so a global field-name
exception set is not a disposition. `goal.measurement` and session `session_id` /
`measures` are contextually forbidden and the gateway refuses them; `operation`
must be `add` and is then translation-dropped. Inventory alone is not the witness
-- `tests/test_gateway.py` holds the prepare refusals.

Issue #427.
"""

from __future__ import annotations

import unittest
from typing import Any

from garmin_coach_loop import plan_change, plan_init, validation
from garmin_coach_loop.gateway import CoachGateway
from garmin_coach_loop.mcp_transport import TOOLS_BY_NAME


ACCEPTED = "accepted"
TRANSLATED = "translated"
FORBIDDEN = "forbidden"
OPAQUE = "opaque"
DISPOSITIONS = {ACCEPTED, TRANSLATED, FORBIDDEN, OPAQUE}

_CHANGE_ONLY = "a first plan states what it is, not what it changed"
_NO_PLAN_YET = "this account has no plan yet, so the field names one that does not exist"
_MEASUREMENT = (
    "reference_session_id has to name a session that exists; on a first plan every "
    "session is one this same request is creating, and its id is derived after the "
    "request is read"
)
_PLAN_OPAQUE = (
    "first-plan copies plan as an object; validate_plan_state owns the kind-specific "
    "shape after projection"
)


def _schema_root() -> dict[str, Any]:
    tool = TOOLS_BY_NAME["prepareCoachDecision"]
    return getattr(tool, "input_schema", None) or getattr(tool, "inputSchema")


def schema_node(path: tuple[Any, ...]) -> dict[str, Any]:
    """The schema object at this path. `[]` is array items; `oneOf:<kind>` selects a branch."""
    node: Any = _schema_root()
    for step in path:
        if step == "[]":
            node = node["items"]
            continue
        if isinstance(step, str) and step.startswith("oneOf:"):
            kind = step.split(":", 1)[1]
            matched = [
                branch
                for branch in node.get("oneOf") or []
                if kind
                in ((branch.get("properties") or {}).get("kind") or {}).get("enum", [])
            ]
            if len(matched) != 1:
                raise AssertionError(f"no unique oneOf branch {kind!r} at {path!r}")
            node = matched[0]
            continue
        node = node["properties"][step]
    return node


def declared(path: tuple[Any, ...]) -> set[str]:
    """The property names a client is shown at this point in the tool's input schema."""
    return set((schema_node(path).get("properties") or {}).keys())


def _d(kind: str, reason: str) -> tuple[str, str]:
    return (kind, reason)


# Path -> field -> (disposition, reason). Every declared property at the path must appear.
# Reasons are the contract the test is pinning, not commentary.
FIRST_PLAN_INVENTORY: dict[tuple[Any, ...], dict[str, tuple[str, str]]] = {
    (): {
        "plan_id": _d(FORBIDDEN, _NO_PLAN_YET),
        "plan_version": _d(FORBIDDEN, _NO_PLAN_YET),
        "context": _d(
            FORBIDDEN,
            "an empty account has no CoachContext; symptoms belong in red_flags",
        ),
        "red_flags": _d(ACCEPTED, "first-plan symptoms, because no context exists to carry them"),
        "publish_new_workouts": _d(ACCEPTED, "preview flag, same as a later change"),
        "change_request": _d(ACCEPTED, "the one plan-authoring object"),
    },
    ("change_request",): {
        "decision_scope": _d(ACCEPTED, "cycle, or omitted for clients on the previous catalogue"),
        "availability": _d(ACCEPTED, "first-plan only; echoed in the preview"),
        "summary": _d(ACCEPTED, "carried"),
        "reason_codes": _d(FORBIDDEN, _CHANGE_ONLY),
        "evidence": _d(ACCEPTED, "carried"),
        "goal_effect": _d(FORBIDDEN, _CHANGE_ONLY),
        "next_review_condition": _d(FORBIDDEN, _CHANGE_ONLY),
        "unknowns": _d(ACCEPTED, "carried"),
        "sessions": _d(ACCEPTED, "the first week's sessions, every one an add"),
        "goal": _d(ACCEPTED, "carried"),
        "cycle": _d(ACCEPTED, "carried"),
        "athlete_baseline": _d(ACCEPTED, "translated to initialization_request.baselines"),
        "week": _d(ACCEPTED, "intent is required; start is derived and consistency-checked"),
    },
    ("change_request", "availability"): {
        "days": _d(ACCEPTED, "preview-only athlete fact"),
        "equipment": _d(ACCEPTED, "preview-only athlete fact"),
    },
    ("change_request", "evidence", "[]"): {
        "field": _d(ACCEPTED, "carried"),
        "observation": _d(ACCEPTED, "carried"),
    },
    ("change_request", "goal"): {
        "outcome": _d(ACCEPTED, "carried"),
        "measurement_protocol": _d(ACCEPTED, "carried"),
        "measurement": _d(FORBIDDEN, _MEASUREMENT),
    },
    ("change_request", "goal", "measurement"): {
        "reference_session_id": _d(FORBIDDEN, _MEASUREMENT),
        "measurement_week_start": _d(FORBIDDEN, _MEASUREMENT),
        "compare": _d(FORBIDDEN, _MEASUREMENT),
    },
    ("change_request", "goal_effect"): {
        "week": _d(FORBIDDEN, _CHANGE_ONLY),
        "cycle": _d(FORBIDDEN, _CHANGE_ONLY),
    },
    ("change_request", "cycle"): {
        "start": _d(ACCEPTED, "the model says when the block starts"),
        "end": _d(
            TRANSLATED,
            "derived as 28 days from start; omitted is derived, a match is accepted, "
            "a disagreement is refused with both dates",
        ),
        "primary_adaptation": _d(ACCEPTED, "carried"),
        "maintenance_adaptation": _d(ACCEPTED, "carried; null is a real answer"),
        "planned_evidence": _d(ACCEPTED, "carried"),
        "adjust_conditions": _d(ACCEPTED, "carried"),
        "stop_conditions": _d(ACCEPTED, "carried"),
        "outlook": _d(ACCEPTED, "required on a first plan"),
    },
    ("change_request", "cycle", "outlook", "[]"): {
        "week_start": _d(ACCEPTED, "parsed, then checked against the three derived weeks"),
        "intent": _d(ACCEPTED, "carried"),
        "key_sessions": _d(ACCEPTED, "carried"),
        "relation_to_primary": _d(ACCEPTED, "carried"),
    },
    ("change_request", "week"): {
        "start": _d(
            TRANSLATED,
            "derived from cycle.start; omitted is derived, a match is accepted, "
            "a disagreement is refused with both dates",
        ),
        "intent": _d(ACCEPTED, "translated to initialization_request.week_intent"),
    },
    ("change_request", "sessions", "[]"): {
        "operation": _d(
            TRANSLATED,
            "must be add, then dropped; a first plan has nothing to keep, move, reduce or replace",
        ),
        "session_id": _d(
            FORBIDDEN,
            "points at a session the plan does not have yet; the server derives the id",
        ),
        "scheduled_date": _d(ACCEPTED, "carried"),
        "planned_minutes": _d(ACCEPTED, "carried"),
        "plan": _d(ACCEPTED, "required object; nested shape is owned by validate_plan_state"),
        "purpose": _d(ACCEPTED, "carried"),
        "coach_note": _d(ACCEPTED, "optional on every operation, including a first-plan add"),
        "measures": _d(
            FORBIDDEN,
            "names a measurement session that does not exist yet; first-plan goal.measurement is refused for the same reason",
        ),
        "sport": _d(ACCEPTED, "carried"),
        "adaptation": _d(ACCEPTED, "carried"),
        "cost": _d(ACCEPTED, "carried"),
        "body_stress": _d(ACCEPTED, "carried"),
        "priority": _d(ACCEPTED, "carried"),
        "time_window": _d(ACCEPTED, "carried"),
        "fallback": _d(ACCEPTED, "required on add"),
    },
    ("change_request", "sessions", "[]", "plan", "oneOf:time_axis"): {
        "kind": _d(OPAQUE, _PLAN_OPAQUE),
        "name": _d(OPAQUE, _PLAN_OPAQUE),
        "steps": _d(OPAQUE, _PLAN_OPAQUE),
    },
    ("change_request", "sessions", "[]", "plan", "oneOf:movement_list"): {
        "kind": _d(OPAQUE, _PLAN_OPAQUE),
        "movements": _d(OPAQUE, _PLAN_OPAQUE),
    },
    ("change_request", "sessions", "[]", "plan", "oneOf:unstructured"): {
        "kind": _d(OPAQUE, _PLAN_OPAQUE),
    },
    (
        "change_request",
        "sessions",
        "[]",
        "plan",
        "oneOf:movement_list",
        "movements",
        "[]",
    ): {
        "exercise": _d(OPAQUE, _PLAN_OPAQUE),
        "display_name": _d(OPAQUE, _PLAN_OPAQUE),
        "sets": _d(OPAQUE, _PLAN_OPAQUE),
        "reps": _d(OPAQUE, _PLAN_OPAQUE),
        "load_kg": _d(OPAQUE, _PLAN_OPAQUE),
        "assist_kg": _d(OPAQUE, _PLAN_OPAQUE),
        "load_basis": _d(OPAQUE, _PLAN_OPAQUE),
    },
    (
        "change_request",
        "sessions",
        "[]",
        "plan",
        "oneOf:time_axis",
        "steps",
        "[]",
    ): {
        "kind": _d(OPAQUE, _PLAN_OPAQUE),
        "name": _d(OPAQUE, _PLAN_OPAQUE),
        "duration": _d(OPAQUE, _PLAN_OPAQUE),
        "target": _d(OPAQUE, _PLAN_OPAQUE),
        "repetitions": _d(OPAQUE, _PLAN_OPAQUE),
        "steps": _d(OPAQUE, _PLAN_OPAQUE),
    },
    (
        "change_request",
        "sessions",
        "[]",
        "plan",
        "oneOf:time_axis",
        "steps",
        "[]",
        "duration",
    ): {
        "kind": _d(OPAQUE, _PLAN_OPAQUE),
        "seconds": _d(OPAQUE, _PLAN_OPAQUE),
        "meters": _d(OPAQUE, _PLAN_OPAQUE),
    },
    (
        "change_request",
        "sessions",
        "[]",
        "plan",
        "oneOf:time_axis",
        "steps",
        "[]",
        "target",
    ): {
        "kind": _d(OPAQUE, _PLAN_OPAQUE),
        "unit": _d(OPAQUE, _PLAN_OPAQUE),
        "low_seconds_per_km": _d(OPAQUE, _PLAN_OPAQUE),
        "high_seconds_per_km": _d(OPAQUE, _PLAN_OPAQUE),
        "ceiling_bpm": _d(OPAQUE, _PLAN_OPAQUE),
    },
    (
        "change_request",
        "sessions",
        "[]",
        "plan",
        "oneOf:time_axis",
        "steps",
        "[]",
        "steps",
        "[]",
    ): {
        "kind": _d(OPAQUE, _PLAN_OPAQUE),
        "name": _d(OPAQUE, _PLAN_OPAQUE),
        "duration": _d(OPAQUE, _PLAN_OPAQUE),
        "target": _d(OPAQUE, _PLAN_OPAQUE),
    },
    ("change_request", "sessions", "[]", "fallback"): {
        "action": _d(ACCEPTED, "carried"),
        "description": _d(ACCEPTED, "carried"),
    },
    ("change_request", "athlete_baseline"): {
        "threshold_pace_sec_per_km": _d(ACCEPTED, "null when unmeasured"),
        "max_hr": _d(ACCEPTED, "null when unmeasured"),
        "easy_hr_ceiling": _d(ACCEPTED, "null when unmeasured"),
        "max_session_minutes": _d(ACCEPTED, "null when unmeasured"),
        "longest_recent_run_km": _d(ACCEPTED, "null when unmeasured"),
        "weekly_volume_km_4wk_avg": _d(ACCEPTED, "null when unmeasured"),
        "strength_loads": _d(ACCEPTED, "carried"),
    },
    ("change_request", "athlete_baseline", "strength_loads", "[]"): {
        "exercise": _d(ACCEPTED, "required"),
        "load_kg": _d(ACCEPTED, "optional measured column"),
        "assist_kg": _d(ACCEPTED, "optional measured column"),
        "scheme": _d(ACCEPTED, "optional"),
        "display_name": _d(ACCEPTED, "optional matching alias"),
    },
}


# Independent runtime inventories -- never the schema compared to itself.
RUNTIME_KNOWN: dict[tuple[Any, ...], frozenset[str]] = {
    (): frozenset(
        name
        for name in CoachGateway.FIRST_PLAN_FIELDS
        if name not in ("proposal", "confirmed")
    )
    | {"context"},
    ("change_request",): frozenset(
        {
            "decision_scope",
            "availability",
            "summary",
            "evidence",
            "unknowns",
            "sessions",
            "goal",
            "cycle",
            "athlete_baseline",
            "week",
            *CoachGateway.CHANGE_ONLY_FIELDS,
        }
    ),
    ("change_request", "availability"): frozenset(plan_init._AVAILABILITY_FIELDS),
    ("change_request", "evidence", "[]"): frozenset(("field", "observation")),
    ("change_request", "goal"): frozenset(("outcome", "measurement_protocol", "measurement")),
    ("change_request", "goal", "measurement"): frozenset(plan_change._MEASUREMENT_FIELDS),
    ("change_request", "goal_effect"): frozenset(("week", "cycle")),
    ("change_request", "cycle"): frozenset(
        plan_init._CYCLE_REQUIRED + plan_init._CYCLE_OPTIONAL
    ),
    ("change_request", "cycle", "outlook", "[]"): frozenset(plan_change._OUTLOOK_FIELDS),
    ("change_request", "week"): frozenset(plan_change._WEEK_FIELDS),
    ("change_request", "sessions", "[]"): frozenset(
        plan_init._SESSION_REQUIRED
        + plan_init._SESSION_OPTIONAL
        + CoachGateway.FIRST_PLAN_SESSION_TRANSLATED
        + CoachGateway.FIRST_PLAN_SESSION_FORBIDDEN
    ),
    ("change_request", "sessions", "[]", "plan", "oneOf:time_axis"): frozenset(
        validation.SESSION_PLAN_FIELDS["time_axis"]
    ),
    ("change_request", "sessions", "[]", "plan", "oneOf:movement_list"): frozenset(
        validation.SESSION_PLAN_FIELDS["movement_list"]
    ),
    ("change_request", "sessions", "[]", "plan", "oneOf:unstructured"): frozenset(
        validation.SESSION_PLAN_FIELDS["unstructured"]
    ),
    (
        "change_request",
        "sessions",
        "[]",
        "plan",
        "oneOf:movement_list",
        "movements",
        "[]",
    ): frozenset(validation.STRENGTH_MOVEMENT_FIELDS),
    ("change_request", "sessions", "[]", "fallback"): frozenset(plan_change._FALLBACK_FIELDS),
    ("change_request", "athlete_baseline"): frozenset(
        plan_init._BASELINE_INTEGERS + plan_init._BASELINE_NUMBERS + ("strength_loads",)
    ),
    ("change_request", "athlete_baseline", "strength_loads", "[]"): frozenset(
        ("exercise",) + plan_init._STRENGTH_LOAD_OPTIONAL
    ),
}


def _label(path: tuple[Any, ...]) -> str:
    if not path:
        return "prepareCoachDecision"
    parts = []
    for step in path:
        if step == "[]":
            parts[-1] = parts[-1] + "[]"
        else:
            parts.append(str(step))
    return ".".join(parts)


class FirstPlanSchemaConformanceTests(unittest.TestCase):
    """Every declared first-plan field has an intentional runtime disposition."""

    def test_every_inventoried_path_matches_the_declared_properties(self):
        for path, fields in FIRST_PLAN_INVENTORY.items():
            with self.subTest(path=_label(path)):
                promised = declared(path)
                inventoried = set(fields)
                self.assertEqual(
                    set(),
                    promised - inventoried,
                    f"{_label(path)} declares {sorted(promised - inventoried)}, which "
                    "has no first-plan disposition. Classify it as accepted, translated "
                    "and consistency-checked, contextually forbidden, or opaque.",
                )
                self.assertEqual(
                    set(),
                    inventoried - promised,
                    f"{_label(path)} inventory names {sorted(inventoried - promised)}, "
                    "which the schema does not declare",
                )

    def test_every_disposition_is_one_of_the_four_and_has_a_reason(self):
        for path, fields in FIRST_PLAN_INVENTORY.items():
            for name, (kind, reason) in fields.items():
                with self.subTest(path=_label(path), field=name):
                    self.assertIn(kind, DISPOSITIONS)
                    self.assertTrue(reason.strip(), f"{_label(path)}.{name} has an empty reason")

    def test_runtime_known_sets_are_independent_of_the_schema(self):
        """Accepted, translated and forbidden fields must match a runtime inventory.

        Opaque passthrough is allowed to lack a parser-side constant only when the
        PlanState validator is named as the owner. Plan kind and movement shapes still
        pin against validation.py. Workout step/duration/target objects are opaque at
        the first-plan parser; they must still be inventoried so a new schema field
        cannot arrive unnamed.
        """
        opaque_without_runtime = {
            (
                "change_request",
                "sessions",
                "[]",
                "plan",
                "oneOf:time_axis",
                "steps",
                "[]",
            ),
            (
                "change_request",
                "sessions",
                "[]",
                "plan",
                "oneOf:time_axis",
                "steps",
                "[]",
                "duration",
            ),
            (
                "change_request",
                "sessions",
                "[]",
                "plan",
                "oneOf:time_axis",
                "steps",
                "[]",
                "target",
            ),
            (
                "change_request",
                "sessions",
                "[]",
                "plan",
                "oneOf:time_axis",
                "steps",
                "[]",
                "steps",
                "[]",
            ),
        }
        for path, fields in FIRST_PLAN_INVENTORY.items():
            with self.subTest(path=_label(path)):
                known = {
                    name
                    for name, (kind, _reason) in fields.items()
                    if kind != OPAQUE
                }
                runtime = RUNTIME_KNOWN.get(path)
                if path in opaque_without_runtime:
                    self.assertIsNone(runtime)
                    self.assertEqual(set(), known)
                    continue
                self.assertIsNotNone(
                    runtime,
                    f"{_label(path)} needs an independent runtime inventory; "
                    "do not fall back to the schema",
                )
                inventoried_known = {
                    name
                    for name, (kind, _reason) in fields.items()
                    if kind != OPAQUE
                }
                if not inventoried_known:
                    # wholly opaque object: runtime still pins the schema properties
                    self.assertEqual(set(fields), set(runtime or ()))
                    continue
                self.assertEqual(inventoried_known, set(runtime or ()))

    def test_accepted_session_and_cycle_fields_are_the_parser_allow_lists(self):
        cycle = FIRST_PLAN_INVENTORY[("change_request", "cycle")]
        self.assertEqual(
            {name for name, (kind, _) in cycle.items() if kind in {ACCEPTED, TRANSLATED}},
            set(plan_init._CYCLE_REQUIRED) | set(plan_init._CYCLE_OPTIONAL),
        )
        session = FIRST_PLAN_INVENTORY[("change_request", "sessions", "[]")]
        self.assertEqual(
            {name for name, (kind, _) in session.items() if kind == ACCEPTED},
            set(plan_init._SESSION_REQUIRED) | set(plan_init._SESSION_OPTIONAL),
        )
        self.assertEqual(
            {name for name, (kind, _) in session.items() if kind == TRANSLATED},
            set(CoachGateway.FIRST_PLAN_SESSION_TRANSLATED),
        )
        self.assertEqual(
            {name for name, (kind, _) in session.items() if kind == FORBIDDEN},
            set(CoachGateway.FIRST_PLAN_SESSION_FORBIDDEN),
        )

    def test_outlook_runtime_inventory_is_plan_change_not_the_schema(self):
        self.assertEqual(
            set(plan_change._OUTLOOK_FIELDS),
            declared(("change_request", "cycle", "outlook", "[]")),
        )

    def test_first_plan_does_not_accept_goal_measurement(self):
        goal = FIRST_PLAN_INVENTORY[("change_request", "goal")]
        self.assertEqual(FORBIDDEN, goal["measurement"][0])

    def test_every_inventoried_path_is_reachable(self):
        for path in FIRST_PLAN_INVENTORY:
            with self.subTest(path=_label(path)):
                schema_node(path)

    def test_runtime_known_has_no_stale_paths(self):
        self.assertEqual(
            set(),
            set(RUNTIME_KNOWN) - set(FIRST_PLAN_INVENTORY),
            "RUNTIME_KNOWN names a path the inventory does not",
        )


if __name__ == "__main__":
    unittest.main()
