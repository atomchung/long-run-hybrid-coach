"""``contracts/pre-plan-observations.schema.json`` held against reads that produce it.

The contract exists so a first-plan eval case can name the evidence it is about
(``tests/test_evals.py`` resolves those paths against it). A schema nothing validates
would let that naming outlive the shape: the case would keep resolving, the eval would
keep reporting itself anchored, and the field it named would have moved. So the same
document is checked the other way round here -- against what the gateway actually emits.

Two reads, because one is not enough. The committed no-plan snapshots are the frozen
answer and cover the shape an account with nothing stated gets, which is both of them
carrying ``athlete_evidence: null``; a live read with every kind of evidence on record
covers the half those snapshots leave undescribed. Between them every branch the contract
declares is instantiated by something a real read produced.

The validator below is a deliberate subset of JSON Schema -- ``$ref`` across files,
``anyOf``, ``type``, ``enum``, ``required``, ``properties``, ``additionalProperties:
false``, ``items``, and the numeric and length bounds -- because that is the whole
vocabulary these contracts use, and this suite runs on a bare Python 3.11 with nothing
installed (AGENTS.md). ``format`` is documentation here, exactly as it is to a validator
run without a format checker.
"""

from __future__ import annotations

import copy
import json
import unittest
from pathlib import Path
from typing import Any

from tests.coach_session_scenarios import MANIFEST_NAME, SNAPSHOTS

from test_gateway import GatewayTestCase, TOKEN_A, recovery_signals_upload


ROOT = Path(__file__).resolve().parents[1]
CONTRACTS = ROOT / "contracts"
SCHEMA_NAME = "pre-plan-observations.schema.json"

DOCUMENTS: dict[str, dict[str, Any]] = {
    path.name: json.loads(path.read_text(encoding="utf-8"))
    for path in sorted(CONTRACTS.glob("*.schema.json"))
}
SCHEMA = DOCUMENTS[SCHEMA_NAME]

_TYPES = {
    "object": lambda value: isinstance(value, dict),
    "array": lambda value: isinstance(value, list),
    "string": lambda value: isinstance(value, str),
    "integer": lambda value: isinstance(value, int) and not isinstance(value, bool),
    "number": lambda value: isinstance(value, (int, float)) and not isinstance(value, bool),
    "boolean": lambda value: isinstance(value, bool),
    "null": lambda value: value is None,
}


def _pointed_at(document: dict[str, Any], pointer: str) -> Any:
    node: Any = document
    for token in pointer.split("/"):
        if not token:
            continue
        node = node.get(token) if isinstance(node, dict) else None
    return node


def violations(
    instance: Any, schema: Any, *, document: dict[str, Any], path: str = "$"
) -> list[str]:
    """Every way ``instance`` fails ``schema``, in the subset the contracts use."""
    if not isinstance(schema, dict):
        return [f"{path}: no schema to check against"]
    problems: list[str] = []

    reference = schema.get("$ref")
    if isinstance(reference, str):
        file_name, _, pointer = reference.partition("#")
        target = DOCUMENTS[file_name] if file_name else document
        problems.extend(
            violations(instance, _pointed_at(target, pointer), document=target, path=path)
        )

    branches = schema.get("anyOf") or schema.get("oneOf")
    if isinstance(branches, list):
        attempts = [
            violations(instance, branch, document=document, path=path) for branch in branches
        ]
        if attempts and all(attempts):
            # The nearest miss, not the fact of the miss: a union spelling "an object or
            # null" should report what was wrong with the object, or a drifted field
            # arrives as "matched none of the shapes" and says nothing about which.
            problems.extend(min(attempts, key=len))

    declared = schema.get("type")
    if declared is not None:
        names = declared if isinstance(declared, list) else [declared]
        if not any(_TYPES[name](instance) for name in names):
            return problems + [f"{path}: {type(instance).__name__} is not {declared}"]

    if "enum" in schema and instance not in schema["enum"]:
        problems.append(f"{path}: {instance!r} not in {schema['enum']}")
    if "const" in schema and instance != schema["const"]:
        problems.append(f"{path}: {instance!r} is not {schema['const']!r}")

    if isinstance(instance, dict):
        properties = schema.get("properties") or {}
        for name in schema.get("required", ()):
            if name not in instance:
                problems.append(f"{path}: missing required {name!r}")
        for name, value in instance.items():
            if name in properties:
                problems.extend(
                    violations(value, properties[name], document=document, path=f"{path}.{name}")
                )
            elif schema.get("additionalProperties") is False:
                problems.append(f"{path}: unexpected {name!r}")

    if isinstance(instance, list) and "items" in schema:
        for index, item in enumerate(instance):
            problems.extend(
                violations(item, schema["items"], document=document, path=f"{path}[{index}]")
            )

    if isinstance(instance, str) and "minLength" in schema and len(instance) < schema["minLength"]:
        problems.append(f"{path}: shorter than {schema['minLength']}")
    if _TYPES["number"](instance):
        if "minimum" in schema and instance < schema["minimum"]:
            problems.append(f"{path}: below {schema['minimum']}")
        if "maximum" in schema and instance > schema["maximum"]:
            problems.append(f"{path}: above {schema['maximum']}")

    return problems


def _check(instance: Any) -> list[str]:
    return violations(instance, SCHEMA, document=SCHEMA)


def _committed_observations() -> dict[str, Any]:
    """Every committed snapshot that carries a no-plan read, by scenario name."""
    found: dict[str, Any] = {}
    for path in sorted(SNAPSHOTS.glob("*.json")):
        if path.name == MANIFEST_NAME:
            continue
        response = json.loads(path.read_text(encoding="utf-8")).get("response") or {}
        if "pre_plan_observations" in response:
            found[path.stem] = response["pre_plan_observations"]
    return found


class CommittedNoPlanReadTests(unittest.TestCase):
    def setUp(self) -> None:
        self.observations = _committed_observations()

    def test_there_are_committed_no_plan_reads_to_check(self):
        # Without this the loop below passes by finding nothing, which is exactly what a
        # renamed response field would cause.
        self.assertGreaterEqual(len(self.observations), 2, sorted(self.observations))

    def test_every_committed_no_plan_read_matches_the_contract(self):
        for name, observations in self.observations.items():
            with self.subTest(scenario=name):
                self.assertEqual([], _check(observations))

    def test_the_accounts_with_nothing_stated_are_the_null_branch_and_not_an_empty_one(self):
        """The distinction the contract makes, made against the committed answer too.

        ``null`` is the container being absent; an athlete who stated things that happened
        to be empty would arrive as an object of empty lists. Reading the first as the
        second is how a first conversation ends up asking for what it already has.
        """
        for name, observations in self.observations.items():
            with self.subTest(scenario=name):
                self.assertIsNone(observations["athlete_evidence"])


class LiveNoPlanReadTests(GatewayTestCase):
    """The half the committed snapshots cannot cover: every kind of evidence populated.

    Run rather than replayed, because the drift this is here to catch is in the producer.
    A snapshot proves what the shape was on the day it was blessed; this proves the
    contract still describes what ``_pre_plan_observations`` builds today.
    """

    def setUp(self):
        super().setUp()
        self.seed_owner(TOKEN_A)
        self.fake.activities = [
            {
                "id": "i3001",
                "type": "Run",
                "start_date_local": "2026-08-11T07:00:00",
                "moving_time": 1800,
                "distance": 4200.0,
                "average_speed": 2.33,
                "average_heartrate": 149,
            }
        ]

    def observations(self, body: dict[str, Any] | None = None) -> dict[str, Any]:
        status, payload = self.route("session", body=body or {}, token=TOKEN_A)
        self.assertEqual(200, status, payload)
        self.assertEqual("no_plan_state", payload["status"])
        return payload["pre_plan_observations"]

    def test_a_read_holding_every_kind_of_evidence_matches_the_contract(self):
        for kind, body in (
            ("availability_record", {"recurring": {"available_days": ["mon", "wed", "fri"]}}),
            ("availability_record", {"week": {"unavailable_days": ["wed"], "note": "出差"}}),
            (
                "strength_report",
                {
                    "date": "2026-08-12",
                    "exercise": "bench press",
                    "category": "chest",
                    "sets": [{"set": 1, "weight_kg": 65, "reps": 4}],
                    "notes": ["最後一組有點勉強"],
                },
            ),
            ("body_measurement_record", {"weight_kg": 72.5, "body_fat_pct": 18.0}),
            (
                "activity_summary_record",
                {
                    "date": "2026-08-11",
                    "sport": "running",
                    "duration_minutes": 30,
                    "distance_km": 5.0,
                    "subjective_feel": 3,
                    "note": "手錶沒錄到",
                },
            ),
            (
                "long_term_goal_record",
                {"metric": "體重", "target": "80 kg", "target_date": "2027-01-01", "note": "增肌"},
            ),
            ("training_preference_record", {"topic": "重訓頻率", "statement": "每週想重訓五次"}),
            ("subjective_state_record", {"note": "最近睡不好"}),
            (
                "history_import",
                {
                    "format": "csv",
                    "source_name": "Strava 匯出",
                    "content": (
                        "Activity ID,Activity Date,Activity Name,Activity Type,Elapsed Time,Distance\n"
                        "9001,2026-08-09 06:12:00,晨跑,Run,2700,8.1\n"
                    ),
                },
            ),
        ):
            status, payload = self.route(kind, body=body, token=TOKEN_A)
            self.assertEqual(200, status, (kind, payload))

        observations = self.observations({"recovery_signals": recovery_signals_upload()})

        evidence = observations["athlete_evidence"]
        # The branches this read exists to instantiate. Asserting them by name keeps a
        # conformance pass from being a pass over an empty object.
        self.assertIsNotNone(evidence["availability"]["recurring"])
        self.assertIsNotNone(evidence["availability"]["effective_this_week"])
        for group in (
            "strength_reports",
            "body_measurements",
            "reported_activities",
            "long_term_goals",
            "training_preferences",
            "subjective_states",
        ):
            self.assertTrue(evidence[group], group)
        self.assertTrue(
            [row for row in evidence["reported_activities"] if row["import"] is not None]
        )
        self.assertIsNotNone(observations["recovery_signals"])
        self.assertIsNotNone(observations["recent_training"])

        self.assertEqual([], _check(observations))

    def test_a_read_whose_provider_failed_matches_the_contract_too(self):
        """The null ``recent_training`` branch, from the failure that produces it."""
        self.fake.read_status = 500
        self.route(
            "activity_summary_record",
            body={"date": "2026-08-11", "sport": "running", "duration_minutes": 30},
            token=TOKEN_A,
        )

        observations = self.observations()

        self.assertIsNone(observations["recent_training"])
        # No provider read happened, so no row claims "checked, nothing there": the
        # overlap flag is absent rather than false, which is why it is optional.
        self.assertNotIn(
            "provider_actual_same_day", observations["athlete_evidence"]["reported_activities"][0]
        )
        self.assertEqual([], _check(observations))


class ValidatorTests(unittest.TestCase):
    """Whether a conformance pass means anything.

    A validator that returned no violations would report every read above as matching the
    contract forever, and the schema would be free to describe something else entirely.
    Each case below is one way a read could drift, including through the row shape the
    contract borrows from another file.
    """

    def setUp(self) -> None:
        self.observations = _committed_observations()["09_no_plan__provider_healthy"]
        self.assertEqual([], _check(self.observations))

    def test_a_key_the_contract_does_not_declare_fails(self):
        drifted = dict(self.observations, readiness_estimate=62)
        self.assertTrue(_check(drifted))

    def test_a_missing_key_fails(self):
        drifted = {
            key: value for key, value in self.observations.items() if key != "recovery_signals"
        }
        self.assertTrue(_check(drifted))

    def test_a_value_outside_a_borrowed_vocabulary_fails(self):
        """Which is the cross-file hop working: the sport enum lives in the other file."""
        drifted = copy.deepcopy(self.observations)
        drifted["recent_training"]["recent_actuals"][0]["sport"] = "curling"
        self.assertTrue(_check(drifted))

    def test_a_field_the_borrowed_row_requires_fails_when_it_goes_missing(self):
        drifted = copy.deepcopy(self.observations)
        drifted["recent_training"]["recent_actuals"][0].pop("match_confidence")
        self.assertTrue(_check(drifted))

    def test_a_group_of_the_wrong_shape_fails_rather_than_matching_the_null_branch(self):
        drifted = dict(self.observations, athlete_evidence="none on record")
        self.assertTrue(_check(drifted))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
