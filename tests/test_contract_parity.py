"""Systematic parity sweep between validation.py's field-shape constants and contracts/.

``garmin_coach_loop/validation.py`` holds roughly fifty module-level ``*_FIELDS`` /
``*_REQUIRED_FIELDS`` constants -- the runtime truth for what shape each evidence group,
row and sub-object may take. ``contracts/*.schema.json`` states the same shapes for
readers and eval bindings that never import the validator. Before this sweep, exactly one
of those constants (``RECONCILIATION_ACTUAL_REQUIRED_FIELDS``) was pinned to the schema by
an equality test in ``tests/test_coach_loop.py``, alongside two more pins on the
validator's inline ``cycle_session`` field lists (not module constants); every other
constant relied on whoever edited one side remembering the other (issue #326).

This module inventories every one of those constants and, for each, either:

- pins it against the contract shape it names (``CASES`` below, checked by
  ``ConstantMatchesItsContractShapeTests``) -- a plain two-sided comparison: the
  validator's *required* keys against the schema's ``required``, and the validator's
  *allowed* keys (required + optional) against the schema's ``properties``; or
- lists it in ``DELIBERATE_EXCEPTIONS`` when it was never meant to equal an independent
  schema shape (an internal projection, an order tuple, a materiality filter); or
- lists it in ``KNOWN_DRIFT_EXCEPTIONS`` when this sweep found the validator and a
  contract already disagree. Per the issue's direction, this file does not decide which
  side is wrong -- each is also written up in the pull request that introduced this
  sweep, for a human to resolve.

Two housekeeping tests keep the inventory itself honest as the module changes:
``test_every_field_constant_has_a_case_a_type_check_or_a_documented_exception`` fails
by name on a new constant nobody has triaged yet, and
``test_every_name_this_sweep_tracks_still_exists`` fails on a stale exception entry or
``Case`` naming a constant that was renamed or removed out from under it.

What this sweep is not: it does not run any JSON Schema validator, and it does not
replace the semantic tests elsewhere in this suite (a field being the right *shape* says
nothing about whether its *value* is checked correctly). It only asks whether the two
places that separately declare a shape still declare the same one.
"""

from __future__ import annotations

import json
import re
import unittest
from pathlib import Path
from typing import Any

from garmin_coach_loop import validation


ROOT = Path(__file__).resolve().parents[1]
CONTRACTS = ROOT / "contracts"


def _load(name: str) -> dict[str, Any]:
    value = json.loads((CONTRACTS / name).read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise AssertionError(f"{name} is not a JSON object")
    return value


COACH_CONTEXT = _load("coach-context.schema.json")
PLAN_STATE = _load("plan-state.schema.json")
CD = COACH_CONTEXT["$defs"]
PD = PLAN_STATE["$defs"]


def _object_branch(node: dict[str, Any]) -> dict[str, Any]:
    """Unwrap a nullable ``anyOf``/``oneOf`` node down to its one object branch.

    Every ``observed`` sub-object below is spelled ``anyOf: [{type: null}, {object...}]``
    rather than a bare object, because ``observed`` itself may be null. Comparing a
    constant against the object branch is comparing against the same shape the wrapping
    ``anyOf`` exists to make nullable, not against nullability itself.
    """
    branches = node.get("anyOf") or node.get("oneOf")
    if branches is None:
        return node
    objects = [branch for branch in branches if branch.get("type") == "object"]
    if len(objects) != 1:
        raise AssertionError(f"expected exactly one object branch, found {len(objects)}: {node!r}")
    return objects[0]


class Case:
    """One constant compared against the contract shape it is meant to describe.

    ``required``/``allowed`` are the field sets the validator *actually* enforces at the
    call site this pins -- read from the literal arguments passed to ``_keys``, not from
    the constant's name or its author's comment. Two of ``KNOWN_DRIFT_EXCEPTIONS`` exist
    exactly because those turned out to disagree with each other.

    ``check_allowed=False`` marks a constant that only pins the *required* side: it has
    no companion module constant naming everything the object may additionally carry, so
    there is nothing to compare the schema's ``properties`` against.
    """

    __slots__ = ("label", "required", "allowed", "schema_node", "covers", "check_allowed")

    def __init__(self, label, required, optional, schema_node, covers, check_allowed=True):
        self.label = label
        self.required = frozenset(required)
        self.allowed = frozenset(required) | frozenset(optional)
        self.schema_node = schema_node
        self.covers = frozenset(covers)
        self.check_allowed = check_allowed


# Kind -> the baseline_evidence row def that carries that kind's `observed` sub-shape.
# All seven share the six-field row envelope (BASELINE_EVIDENCE_ROW_FIELDS /
# BASELINE_EVIDENCE_STRENGTH_ROW_FIELDS below); this maps each to the def whose
# `observed` property BASELINE_EVIDENCE_OBSERVED_FIELDS[kind] must equal.
_BASELINE_EVIDENCE_ROW_DEF_BY_KIND = {
    "threshold_pace_sec_per_km": "baseline_evidence_threshold_pace_row",
    "max_hr": "baseline_evidence_max_hr_row",
    "easy_hr_ceiling": "baseline_evidence_easy_hr_row",
    "longest_recent_run_km": "baseline_evidence_longest_run_row",
    "weekly_volume_km_4wk_avg": "baseline_evidence_weekly_volume_row",
    "max_session_minutes": "baseline_evidence_session_minutes_row",
    "strength_loads": "baseline_evidence_strength_row",
}
# The six non-strength row defs, each an exact-fields echo of BASELINE_EVIDENCE_ROW_FIELDS
# (contracts/coach-context.schema.json's `baseline_evidence_row` is the `oneOf` of these
# six plus `baseline_evidence_strength_row`, which BASELINE_EVIDENCE_STRENGTH_ROW_FIELDS
# covers separately below).
_BASELINE_EVIDENCE_NON_STRENGTH_ROW_DEFS = [
    name for kind, name in _BASELINE_EVIDENCE_ROW_DEF_BY_KIND.items() if kind != "strength_loads"
]


def _build_cases() -> list[Case]:
    cases: list[Case] = []

    def add(label, required, optional, schema_node, covers, check_allowed=True):
        cases.append(Case(label, required, optional, schema_node, covers, check_allowed))

    # -- athlete_baseline: shared verbatim by CoachContext and PlanState (validation.py's
    # own docstring on _validate_athlete_baseline says so), each declaring it inline
    # rather than through a $ref, so both copies are checked independently. --
    for schema_name, schema in (("coach-context", COACH_CONTEXT), ("plan-state", PLAN_STATE)):
        add(
            f"ATHLETE_BASELINE_FIELDS ({schema_name})",
            validation.ATHLETE_BASELINE_FIELDS, (),
            schema["properties"]["athlete_baseline"],
            {"ATHLETE_BASELINE_FIELDS"},
        )

    # -- strength_load: the athlete_baseline.strength_loads row, also $ref'd from
    # plan-state's own athlete_baseline. Both files carry the def, so both are checked. --
    for schema_name, defs in (("coach-context", CD), ("plan-state", PD)):
        add(
            f"STRENGTH_LOAD_FIELDS + STRENGTH_LOAD_OPTIONAL_FIELDS ({schema_name})",
            validation.STRENGTH_LOAD_FIELDS, validation.STRENGTH_LOAD_OPTIONAL_FIELDS,
            defs["strength_load"],
            {"STRENGTH_LOAD_FIELDS", "STRENGTH_LOAD_OPTIONAL_FIELDS"},
        )

    # -- the three session-plan models (issue #93), each one PlanState def --
    for kind, def_name in (
        ("time_axis", "plan_time_axis"),
        ("movement_list", "plan_movement_list"),
        ("unstructured", "plan_unstructured"),
    ):
        add(
            f"SESSION_PLAN_FIELDS[{kind!r}]",
            validation.SESSION_PLAN_FIELDS[kind], (),
            PD[def_name],
            {"SESSION_PLAN_FIELDS"},
        )

    add(
        "STRENGTH_MOVEMENT_FIELDS", validation.STRENGTH_MOVEMENT_FIELDS, (),
        PD["strength_movement"], {"STRENGTH_MOVEMENT_FIELDS"},
    )

    # -- strength_execution (issue #37) --
    add(
        "STRENGTH_EXECUTION_FIELDS", validation.STRENGTH_EXECUTION_FIELDS, (),
        CD["strength_execution"], {"STRENGTH_EXECUTION_FIELDS"},
    )
    add(
        "STRENGTH_EXECUTION_SET_FIELDS", validation.STRENGTH_EXECUTION_SET_FIELDS, (),
        CD["strength_execution_set"], {"STRENGTH_EXECUTION_SET_FIELDS"},
    )
    # STRENGTH_EXECUTION_SESSION_FIELDS is a KNOWN_DRIFT_EXCEPTION (the `category` key);
    # its allowed side is still checked in ALLOWED_ONLY_CASES below.

    # -- the reconciliation identity a reduced recent_actuals row keeps (issue #240 §1).
    # Only the required side has an independent module constant: the full `actual` shape's
    # other eleven fields are local variables inside validate_coach_context, not named
    # constants, so there is no companion "allowed" set to compare contracts/
    # coach-context.schema.json's `actual.properties` against here. This duplicates
    # test_the_schema_and_the_validator_name_the_same_reconciliation_identity in
    # tests/test_coach_loop.py by design, so this sweep stays self-sufficient. --
    add(
        "RECONCILIATION_ACTUAL_REQUIRED_FIELDS",
        validation.RECONCILIATION_ACTUAL_REQUIRED_FIELDS, (),
        CD["actual"], {"RECONCILIATION_ACTUAL_REQUIRED_FIELDS"},
        check_allowed=False,
    )

    # -- movement_history (issue #37 lineage) --
    add(
        "MOVEMENT_HISTORY_FIELDS", validation.MOVEMENT_HISTORY_FIELDS, (),
        CD["movement_history"], {"MOVEMENT_HISTORY_FIELDS"},
    )
    add(
        "MOVEMENT_HISTORY_MOVEMENT_FIELDS", validation.MOVEMENT_HISTORY_MOVEMENT_FIELDS, (),
        CD["movement_history_movement"], {"MOVEMENT_HISTORY_MOVEMENT_FIELDS"},
    )
    add(
        "MOVEMENT_HISTORY_BASELINE_FIELDS", validation.MOVEMENT_HISTORY_BASELINE_FIELDS, (),
        CD["movement_history_baseline"], {"MOVEMENT_HISTORY_BASELINE_FIELDS"},
    )
    add(
        "MOVEMENT_HISTORY_OCCURRENCE_FIELDS + _OPTIONAL_FIELDS",
        validation.MOVEMENT_HISTORY_OCCURRENCE_FIELDS,
        validation.MOVEMENT_HISTORY_OCCURRENCE_OPTIONAL_FIELDS,
        CD["movement_history_occurrence"],
        {"MOVEMENT_HISTORY_OCCURRENCE_FIELDS", "MOVEMENT_HISTORY_OCCURRENCE_OPTIONAL_FIELDS"},
    )
    add(
        "MOVEMENT_HISTORY_PRESCRIPTION_FIELDS", validation.MOVEMENT_HISTORY_PRESCRIPTION_FIELDS, (),
        CD["movement_history_prescription"], {"MOVEMENT_HISTORY_PRESCRIPTION_FIELDS"},
    )
    add(
        "MOVEMENT_HISTORY_LOAD_ROLLUP_FIELDS", validation.MOVEMENT_HISTORY_LOAD_ROLLUP_FIELDS, (),
        CD["movement_history_load_rollup"], {"MOVEMENT_HISTORY_LOAD_ROLLUP_FIELDS"},
    )
    add(
        "MOVEMENT_HISTORY_LOAD_ROLLUP_ROW_FIELDS", validation.MOVEMENT_HISTORY_LOAD_ROLLUP_ROW_FIELDS, (),
        CD["movement_history_load_rollup_row"], {"MOVEMENT_HISTORY_LOAD_ROLLUP_ROW_FIELDS"},
    )
    add(
        "MOVEMENT_HISTORY_TOP_LOAD_FIELDS", validation.MOVEMENT_HISTORY_TOP_LOAD_FIELDS, (),
        CD["movement_history_top_load"], {"MOVEMENT_HISTORY_TOP_LOAD_FIELDS"},
    )

    # -- baseline_evidence (issue #32) --
    for def_name in _BASELINE_EVIDENCE_NON_STRENGTH_ROW_DEFS:
        add(
            f"BASELINE_EVIDENCE_ROW_FIELDS ({def_name})",
            validation.BASELINE_EVIDENCE_ROW_FIELDS, (),
            CD[def_name],
            {"BASELINE_EVIDENCE_ROW_FIELDS"},
        )
    add(
        "BASELINE_EVIDENCE_STRENGTH_ROW_FIELDS", validation.BASELINE_EVIDENCE_STRENGTH_ROW_FIELDS, (),
        CD["baseline_evidence_strength_row"], {"BASELINE_EVIDENCE_STRENGTH_ROW_FIELDS"},
    )
    add(
        "BASELINE_EVIDENCE_WEEK_FIELDS", validation.BASELINE_EVIDENCE_WEEK_FIELDS, (),
        CD["baseline_evidence_week"], {"BASELINE_EVIDENCE_WEEK_FIELDS"},
    )
    add(
        "BASELINE_EVIDENCE_LOAD_FIELDS", validation.BASELINE_EVIDENCE_LOAD_FIELDS, (),
        CD["baseline_evidence_load"], {"BASELINE_EVIDENCE_LOAD_FIELDS"},
    )
    for kind, def_name in _BASELINE_EVIDENCE_ROW_DEF_BY_KIND.items():
        observed = _object_branch(CD[def_name]["properties"]["observed"])
        add(
            f"BASELINE_EVIDENCE_OBSERVED_FIELDS[{kind!r}]",
            validation.BASELINE_EVIDENCE_OBSERVED_FIELDS[kind], (),
            observed,
            {"BASELINE_EVIDENCE_OBSERVED_FIELDS"},
        )

    # -- per-sample fields inside a full-detail segment_execution activity. The group and
    # activity envelopes around it (SEGMENT_EXECUTION_FIELDS, SEGMENT_EXECUTION_ACTIVITY_
    # FIELDS/_OPTIONAL_FIELDS, SEGMENT_ROW_FIELDS) are KNOWN_DRIFT_EXCEPTIONS below. --
    add(
        "SEGMENT_EXECUTION_SEGMENT_FIELDS", validation.SEGMENT_EXECUTION_SEGMENT_FIELDS, (),
        CD["segment_execution_segment"], {"SEGMENT_EXECUTION_SEGMENT_FIELDS"},
    )

    # -- run_drift / set_structure (a session's start read against its end) --
    add(
        "RUN_DRIFT_FIELDS", validation.RUN_DRIFT_FIELDS, (),
        CD["run_drift"], {"RUN_DRIFT_FIELDS"},
    )
    add(
        "RUN_DRIFT_ACTIVITY_FIELDS", validation.RUN_DRIFT_ACTIVITY_FIELDS, (),
        CD["run_drift_activity"], {"RUN_DRIFT_ACTIVITY_FIELDS"},
    )
    # RUN_DRIFT_END_FIELDS is a KNOWN_DRIFT_EXCEPTION (see below): the call site's
    # `optional=` is a no-op, so today every one of its four fields is required.
    add(
        "SET_STRUCTURE_FIELDS", validation.SET_STRUCTURE_FIELDS, (),
        CD["set_structure"], {"SET_STRUCTURE_FIELDS"},
    )
    # SET_STRUCTURE_ACTIVITY_FIELDS is the same KNOWN_DRIFT_EXCEPTION shape as
    # RUN_DRIFT_END_FIELDS -- see below.

    # -- the standalone conversational/reported evidence groups --
    add(
        "BODY_MEASUREMENTS_FIELDS", validation.BODY_MEASUREMENTS_FIELDS, (),
        CD["body_measurements"], {"BODY_MEASUREMENTS_FIELDS"},
    )
    add(
        "BODY_MEASUREMENT_FIELDS", validation.BODY_MEASUREMENT_FIELDS, (),
        CD["body_measurement"], {"BODY_MEASUREMENT_FIELDS"},
    )
    add(
        "REPORTED_ACTIVITIES_FIELDS", validation.REPORTED_ACTIVITIES_FIELDS, (),
        CD["reported_activities"], {"REPORTED_ACTIVITIES_FIELDS"},
    )
    add(
        "REPORTED_ACTIVITY_FIELDS", validation.REPORTED_ACTIVITY_FIELDS, (),
        CD["reported_activity"], {"REPORTED_ACTIVITY_FIELDS"},
    )
    add(
        "SUBJECTIVE_STATES_FIELDS", validation.SUBJECTIVE_STATES_FIELDS, (),
        CD["subjective_states"], {"SUBJECTIVE_STATES_FIELDS"},
    )
    add(
        "SUBJECTIVE_STATE_FIELDS", validation.SUBJECTIVE_STATE_FIELDS, (),
        # Inline under subjective_states.properties.states.items rather than its own
        # $defs entry -- $defs["subjective"] is an unrelated enum (leg_fatigue/soreness).
        CD["subjective_states"]["properties"]["states"]["items"],
        {"SUBJECTIVE_STATE_FIELDS"},
    )

    # -- recovery_signals (issue #37 slice 2). The per-day group is a KNOWN_DRIFT_EXCEPTION
    # pair below (issue #187 loosened the validator without updating the schema). --
    add(
        "RECOVERY_SIGNALS_FIELDS", validation.RECOVERY_SIGNALS_FIELDS, (),
        CD["recovery_signals"], {"RECOVERY_SIGNALS_FIELDS"},
    )

    # -- training_history (issue #101) --
    add(
        "TRAINING_HISTORY_FIELDS", validation.TRAINING_HISTORY_FIELDS, (),
        CD["training_history"], {"TRAINING_HISTORY_FIELDS"},
    )
    add(
        "TRAINING_HISTORY_MONTH_FIELDS", validation.TRAINING_HISTORY_MONTH_FIELDS, (),
        CD["training_history_month"], {"TRAINING_HISTORY_MONTH_FIELDS"},
    )
    add(
        "TRAINING_HISTORY_PROVENANCE_FIELDS", validation.TRAINING_HISTORY_PROVENANCE_FIELDS, (),
        CD["training_history_provenance_counts"], {"TRAINING_HISTORY_PROVENANCE_FIELDS"},
    )
    add(
        "TRAINING_HISTORY_MOVEMENT_FIELDS", validation.TRAINING_HISTORY_MOVEMENT_FIELDS, (),
        CD["training_history_movement"], {"TRAINING_HISTORY_MOVEMENT_FIELDS"},
    )
    add(
        "TRAINING_HISTORY_OBSERVATION_FIELDS + _OPTIONAL_FIELDS",
        validation.TRAINING_HISTORY_OBSERVATION_FIELDS,
        validation.TRAINING_HISTORY_OBSERVATION_OPTIONAL_FIELDS,
        CD["training_history_observation"],
        {"TRAINING_HISTORY_OBSERVATION_FIELDS", "TRAINING_HISTORY_OBSERVATION_OPTIONAL_FIELDS"},
    )

    # -- evidence_expectations (issue #28) --
    add(
        "EVIDENCE_EXPECTATIONS_FIELDS", validation.EVIDENCE_EXPECTATIONS_FIELDS, (),
        CD["evidence_expectations"], {"EVIDENCE_EXPECTATIONS_FIELDS"},
    )
    add(
        "EVIDENCE_EXPECTATION_FIELDS + _OPTIONAL_FIELDS",
        validation.EVIDENCE_EXPECTATION_FIELDS,
        validation.EVIDENCE_EXPECTATION_OPTIONAL_FIELDS,
        CD["evidence_expectation_stream"],
        {"EVIDENCE_EXPECTATION_FIELDS", "EVIDENCE_EXPECTATION_OPTIONAL_FIELDS"},
    )

    return cases


CASES: list[Case] = _build_cases()


class AllowedOnlyCase:
    """The half of a known-drift pair that still has a clean contract counterpart.

    Each constant here is excepted in ``KNOWN_DRIFT_EXCEPTIONS`` because the validator's
    *required* set has already drifted from the schema's ``required``. That does not make
    the rest of the comparison worthless: the *allowed* set -- every key the validator
    will accept, required or not -- still matches the schema's declared ``properties``,
    and dropping that half too would hide a second, independent drift behind the first.
    """

    __slots__ = ("label", "allowed", "schema_properties")

    def __init__(self, label, keys, schema_properties):
        self.label = label
        self.allowed = frozenset(keys)
        self.schema_properties = frozenset(schema_properties)


ALLOWED_ONLY_CASES: list[AllowedOnlyCase] = [
    AllowedOnlyCase(
        "STRENGTH_EXECUTION_SESSION_FIELDS",
        validation.STRENGTH_EXECUTION_SESSION_FIELDS,
        CD["strength_execution_session"]["properties"],
    ),
    AllowedOnlyCase(
        "RUN_DRIFT_END_FIELDS",
        validation.RUN_DRIFT_END_FIELDS,
        CD["run_drift_end"]["properties"],
    ),
    AllowedOnlyCase(
        "SET_STRUCTURE_ACTIVITY_FIELDS",
        validation.SET_STRUCTURE_ACTIVITY_FIELDS,
        CD["set_structure_activity"]["properties"],
    ),
    AllowedOnlyCase(
        "RECOVERY_SIGNALS_DAY_FIELDS",
        validation.RECOVERY_SIGNALS_DAY_FIELDS,
        CD["recovery_signals_day"]["properties"],
    ),
]


# Constants that were never meant to equal an independent contract shape.
DELIBERATE_EXCEPTIONS: dict[str, str] = {
    "MATERIAL_SESSION_FIELDS": (
        "Used only by _check_change_is_material to decide whether an edit between two "
        "plan versions touched anything that matters -- a curated subset of the "
        "plan-state `session` shape's own fields (itself checked against an inline "
        "tuple, not a module constant). A materiality filter, not a shape; there is no "
        "contract node whose properties or required this could equal."
    ),
    "RECONCILIATION_ACTUAL_FIELDS": (
        "Used only by context_core.py's reduced-row builder "
        "(`{key: actual.get(key) for key in RECONCILIATION_ACTUAL_FIELDS}`), a strict "
        "projection of the `actual` shape's full eighteen properties down to the eight "
        "that survive reduction (issue #240 section 1). RECONCILIATION_ACTUAL_REQUIRED_"
        "FIELDS, its base, is the piece pinned against contracts/coach-context.schema."
        "json's `actual.required` directly (see CASES above); the `actual` schema node "
        "describes one object that may carry either the full or the reduced shape, so "
        "the reduced projection has no independent properties/required pair of its own."
    ),
}

# Constants where this sweep found the validator and a contract already disagree.
# Per the issue's direction, this file does not fix either side -- it names the
# disagreement so a human can decide which one is wrong. Each is also written up in the
# pull request that introduced this sweep.
KNOWN_DRIFT_EXCEPTIONS: dict[str, str] = {
    "STRENGTH_EXECUTION_SESSION_FIELDS": (
        "_validate_strength_execution_session calls _keys with this whole tuple as "
        "`required` and no `optional=`, so the validator demands the `category` key be "
        "present (its value may still be null) on every strength_execution session row. "
        "contracts/coach-context.schema.json's strength_execution_session lists "
        "`category` in `properties` but leaves it out of `required` -- a row that omits "
        "the key validates against the schema and fails the validator. Its allowed side "
        "still matches (see ALLOWED_ONLY_CASES)."
    ),
    "SEGMENT_EXECUTION_FIELDS": (
        "_validate_segment_execution requires `full_detail_start` as a key on every "
        "group. contracts/coach-context.schema.json's segment_execution has no "
        "`full_detail_start` property at all and sets additionalProperties: false -- so "
        "a group carrying the key, which the validator demands, is schema-invalid, and "
        "a group without it, which the schema demands, fails the validator. Nothing can "
        "satisfy both today, so there is no allowed-side check to salvage either."
    ),
    "SEGMENT_EXECUTION_ACTIVITY_FIELDS": (
        "Only activity_id/date/sport are unconditionally required; `segments` moves to "
        "SEGMENT_EXECUTION_ACTIVITY_OPTIONAL_FIELDS for a segment_rows-based (compact, "
        "issue #290) activity. contracts/coach-context.schema.json's "
        "segment_execution_activity instead lists `segments` in `required` "
        "unconditionally."
    ),
    "SEGMENT_EXECUTION_ACTIVITY_OPTIONAL_FIELDS": (
        "Names the compact row shape a session behind the full-detail window uses "
        "(issue #290): `segment_fields` + `segment_rows` beside `recorded_indoors` and "
        "(conditionally) `segments`. contracts/coach-context.schema.json's "
        "segment_execution_activity has neither `segment_fields` nor `segment_rows` as "
        "a property, and additionalProperties: false means a compact-shape activity the "
        "validator accepts is schema-invalid."
    ),
    "SEGMENT_ROW_FIELDS": (
        "The declared column order for one segment_rows entry (_validate_segment_rows "
        "checks it positionally). There is no contract counterpart for the same reason "
        "SEGMENT_EXECUTION_ACTIVITY_OPTIONAL_FIELDS has none: the compact shape it "
        "describes is not in contracts/coach-context.schema.json at all yet."
    ),
    "RUN_DRIFT_END_FIELDS": (
        "_validate_run_drift_end calls _keys with this whole tuple as `required` and "
        "`optional=frozenset(RUN_DRIFT_END_FIELDS[1:])` -- but _keys's `optional` can "
        "only add allowed keys beyond `required`, never remove from it, so passing an "
        "already-required subset back in as `optional` is a no-op: all four fields are "
        "required today. That contradicts both the function's own comment ('Only heart "
        "rate is required...') and contracts/coach-context.schema.json's "
        "run_drift_end.required, which is ['average_hr'] alone -- the comment and the "
        "schema agree with each other and disagree with the code, which is what a stray "
        "`required=RUN_DRIFT_END_FIELDS` (instead of `RUN_DRIFT_END_FIELDS[:1]`) would "
        "produce. Its allowed side still matches (see ALLOWED_ONLY_CASES)."
    ),
    "SET_STRUCTURE_ACTIVITY_FIELDS": (
        "The same _keys(FULL_TUPLE, optional=frozenset(FULL_TUPLE[5:])) pattern as "
        "RUN_DRIFT_END_FIELDS above, and the same bug: `optional` is a no-op over a "
        "subset of `required`, so all nine fields are required today, contradicting the "
        "function's own comment ('The four drift values are absent...never zeroed') and "
        "contracts/coach-context.schema.json's set_structure_activity.required, which "
        "lists only the first five. Its allowed side still matches (see "
        "ALLOWED_ONLY_CASES)."
    ),
    "RECOVERY_SIGNALS_DAY_OBSERVATION_FIELDS": (
        "_validate_recovery_signals_day requires only `date` and passes this whole "
        "constant as `optional=` (issue #187: 'a missing key and an explicit null say "
        "the same thing'). contracts/coach-context.schema.json's "
        "recovery_signals_day.required still lists nine of these fourteen fields as "
        "mandatory -- the shape from before that change."
    ),
    "RECOVERY_SIGNALS_DAY_FIELDS": (
        "gateway.py's fill-the-absent-readings-in projection of one full day (`date` "
        "plus every RECOVERY_SIGNALS_DAY_OBSERVATION_FIELDS reading, always present "
        "with null where unobserved). Its key set matches recovery_signals_day."
        "properties exactly (see ALLOWED_ONLY_CASES); see "
        "RECOVERY_SIGNALS_DAY_OBSERVATION_FIELDS above for the same schema node's "
        "required-side drift."
    ),
}

# The two type-partition constants: not compared against a schema shape at all (they
# have none of their own), but against the JSON *type* the schema declares for each
# field they name. See AthleteBaselineTypePartitionTests.
TYPE_CHECKED: frozenset[str] = frozenset(
    {"ATHLETE_BASELINE_INTEGER_FIELDS", "ATHLETE_BASELINE_NUMBER_FIELDS"}
)


class ConstantMatchesItsContractShapeTests(unittest.TestCase):
    """Each ``Case`` pins one validator field-set against the contract shape it names.

    ``required`` is compared against the schema's own ``required`` list, and
    ``required | optional`` (every key the validator will accept) against the schema's
    ``properties`` -- the same two-sided comparison the three original pins used,
    generalized to every constant in the module that has a clean counterpart.
    """

    def test_required_and_allowed_keys_match_the_schema(self):
        self.assertTrue(CASES, "the parity case list must not be empty")
        for parity_case in CASES:
            with self.subTest(case=parity_case.label):
                self.assertEqual(
                    parity_case.required,
                    frozenset(parity_case.schema_node["required"]),
                    f"{parity_case.label}: the validator's required keys drifted from "
                    "the contract's \"required\" list",
                )
                if parity_case.check_allowed:
                    self.assertEqual(
                        parity_case.allowed,
                        frozenset(parity_case.schema_node["properties"]),
                        f"{parity_case.label}: the validator's required+optional keys "
                        "drifted from the contract's \"properties\"",
                    )


class KnownDriftAllowedKeysStillCheckedTests(unittest.TestCase):
    def test_allowed_keys_still_match_the_schema_properties(self):
        self.assertTrue(ALLOWED_ONLY_CASES)
        for allowed_case in ALLOWED_ONLY_CASES:
            with self.subTest(case=allowed_case.label):
                self.assertEqual(
                    allowed_case.allowed,
                    allowed_case.schema_properties,
                    f"{allowed_case.label}: the validator's allowed keys drifted from "
                    "the contract's \"properties\" too, on top of the already-known "
                    "required-side drift",
                )


class AthleteBaselineTypePartitionTests(unittest.TestCase):
    """ATHLETE_BASELINE_INTEGER_FIELDS / _NUMBER_FIELDS split ATHLETE_BASELINE_FIELDS's
    seven scalar-or-list fields by the JSON type each carries, rather than naming an
    independent contract shape -- so parity here means the split is exhaustive and every
    named field actually carries the declared type in both contracts.
    """

    def test_the_two_type_groups_exactly_partition_the_scalar_baseline_fields(self):
        scalar_fields = set(validation.ATHLETE_BASELINE_FIELDS) - {"strength_loads"}
        integer_fields = set(validation.ATHLETE_BASELINE_INTEGER_FIELDS)
        number_fields = set(validation.ATHLETE_BASELINE_NUMBER_FIELDS)
        self.assertEqual(set(), integer_fields & number_fields, "a field cannot be both")
        self.assertEqual(
            scalar_fields, integer_fields | number_fields,
            "every non-strength_loads athlete_baseline field must be typed exactly once",
        )

    def test_each_group_matches_both_contracts_declared_json_type(self):
        for schema_name, schema in (("coach-context", COACH_CONTEXT), ("plan-state", PLAN_STATE)):
            properties = schema["properties"]["athlete_baseline"]["properties"]
            for name in validation.ATHLETE_BASELINE_INTEGER_FIELDS:
                with self.subTest(schema=schema_name, field=name, expect="integer"):
                    self.assertIn("integer", properties[name]["type"])
            for name in validation.ATHLETE_BASELINE_NUMBER_FIELDS:
                with self.subTest(schema=schema_name, field=name, expect="number"):
                    self.assertIn("number", properties[name]["type"])


class DocumentedExceptionsTests(unittest.TestCase):
    def test_every_exception_names_a_constant_that_still_exists(self):
        for name in {**DELIBERATE_EXCEPTIONS, **KNOWN_DRIFT_EXCEPTIONS}:
            with self.subTest(name=name):
                self.assertTrue(
                    hasattr(validation, name),
                    f"{name} is listed as an exception but no longer exists in "
                    "validation.py -- remove the stale entry",
                )

    def test_no_name_is_both_a_deliberate_and_a_drift_exception(self):
        overlap = set(DELIBERATE_EXCEPTIONS) & set(KNOWN_DRIFT_EXCEPTIONS)
        self.assertEqual(set(), overlap)


_FIELDS_CONSTANT_NAME = re.compile(r"^[A-Z][A-Z0-9_]*_FIELDS$")


def _module_field_constant_names() -> set[str]:
    """Every module-level ``*_FIELDS``/``*_REQUIRED_FIELDS`` name in validation.py.

    Both suffixes are covered by one pattern: ``*_REQUIRED_FIELDS`` already ends in
    ``_FIELDS``. Reading ``vars(validation)`` rather than re-parsing the source means
    this reflects the module as Python actually loaded it, not a second copy of the
    naming convention that could itself drift from the constants it is meant to find.
    """
    return {name for name in vars(validation) if _FIELDS_CONSTANT_NAME.match(name)}


def _covered_names() -> set[str]:
    covered: set[str] = set()
    for parity_case in CASES:
        covered |= parity_case.covers
    return covered


class EveryModuleFieldConstantIsAccountedForTests(unittest.TestCase):
    """The roundtrip that keeps this whole sweep from going stale itself.

    A new ``*_FIELDS`` constant in validation.py starts out in neither ``CASES`` nor
    either exception dict, so it fails the first test below by name until someone
    triages it -- into a ``Case``, into ``TYPE_CHECKED``, or into an exception with a
    reason. A constant this sweep references that validation.py no longer defines (renamed
    or removed) fails the second test the same way.
    """

    def test_every_field_constant_has_a_case_a_type_check_or_a_documented_exception(self):
        accounted_for = (
            _covered_names()
            | TYPE_CHECKED
            | set(DELIBERATE_EXCEPTIONS)
            | set(KNOWN_DRIFT_EXCEPTIONS)
        )
        missing = _module_field_constant_names() - accounted_for
        self.assertEqual(
            set(), missing,
            "validation.py has a *_FIELDS/*_REQUIRED_FIELDS constant this sweep does "
            "not yet compare against any contract shape or list as a documented "
            "exception: " + ", ".join(sorted(missing)),
        )

    def test_every_name_this_sweep_tracks_still_exists(self):
        accounted_for = (
            _covered_names()
            | TYPE_CHECKED
            | set(DELIBERATE_EXCEPTIONS)
            | set(KNOWN_DRIFT_EXCEPTIONS)
        )
        stale = accounted_for - _module_field_constant_names()
        self.assertEqual(
            set(), stale,
            "this sweep references a constant validation.py no longer defines -- it "
            "was likely renamed or removed without updating "
            "tests/test_contract_parity.py: " + ", ".join(sorted(stale)),
        )


if __name__ == "__main__":
    unittest.main()
