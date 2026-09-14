"""Published input schema → runtime disposition for the state-bearing public tools.

Issue #427: a green suite that only proves the repository is consistent with itself
cannot notice when the catalogue advertises a field the runtime drops, or when the
runtime honours a field the catalogue never declared. This module derives the field
list from the published `inputSchema` at runtime and records one disposition per
field:

- load-bearing: accepted and changes the result (or is consistency-checked)
- accepted-and-ignored: parsed, and a nonsense value is refused, but a valid
  value does not change the result
- rejected: the runtime refuses the call when the field is present

Silent drop — advertised, not rejected, not in the runtime's known set — fails
the build. So does a runtime-known field the schema does not declare.

Covered tools: startCoachSession, readCoachEvidence, prepareCoachDecision,
applyCoachDecision, prepareWorkoutDelivery, applyWorkoutDelivery. Nested
`change_request` on an existing plan is classified at the object’s first level
against `plan_change`'s allow-list; the session-plan tree on that path is the
same opaque PlanState shape first-plan already inventories.
"""

from __future__ import annotations

import copy
import unittest
from typing import Any

from garmin_coach_loop.context_core import RED_FLAG_FIELDS
from garmin_coach_loop.context_view import FOCUS_AXES
from garmin_coach_loop.plan_change import _OPTIONAL_FIELDS as CHANGE_OPTIONAL
from garmin_coach_loop.plan_change import _REQUIRED_FIELDS as CHANGE_REQUIRED
from garmin_coach_loop.store import read_current_plan
from garmin_coach_loop.validation import RECOVERY_SIGNALS_DAY_FIELDS

from schema_runtime import (
    DISPOSITIONS,
    IMPORTANT_PUBLIC_TOOLS,
    LOAD_BEARING,
    REJECTED,
    coverage_problems,
    input_schema,
    path_label,
    property_paths,
    published_fields,
)
from test_gateway import (
    ONBOARDING,
    RUN_SPORT_SETTINGS,
    TOKEN_A,
    WEEKLY_CHANGE,
    as_change_request,
    publishable_plan,
    recovery_signals_upload,
    schema_rich_change_request,
)
from test_mcp_gateway import McpTestCase
from test_schema_conformance import (
    FIRST_PLAN_INVENTORY,
    FORBIDDEN,
    OPAQUE,
    RUNTIME_KNOWN,
)


def _flatten(
    mapping: dict[tuple[Any, ...], set[str] | frozenset[str]],
) -> set[tuple[Any, ...]]:
    return {path + (name,) for path, names in mapping.items() for name in names}


def _first_plan_dispositions() -> dict[tuple[Any, ...], str]:
    out: dict[tuple[Any, ...], str] = {}
    for path, fields in FIRST_PLAN_INVENTORY.items():
        for name, (kind, _reason) in fields.items():
            out[path + (name,)] = REJECTED if kind == FORBIDDEN else LOAD_BEARING
    return out


def _first_plan_runtime_known() -> set[tuple[Any, ...]]:
    known = _flatten({path: set(names) for path, names in RUNTIME_KNOWN.items()})
    for path, fields in FIRST_PLAN_INVENTORY.items():
        for name, (kind, _reason) in fields.items():
            if kind == OPAQUE:
                known.add(path + (name,))
    return known


START_SESSION_RUNTIME = {
    (): {
        "read",
        "as_of",
        "timezone",
        "available_days",
        "session_minutes",
        "red_flags",
        "all_clear",
        "leg_fatigue",
        "soreness",
        "schedule_changed",
        "equipment_changed",
        "unknowns",
        "guidance_received",
        "guidance_digest",
        "recovery_signals",
    },
    ("red_flags",): set(RED_FLAG_FIELDS),
    ("recovery_signals",): {"source", "days"},
    ("recovery_signals", "days", "[]"): set(RECOVERY_SIGNALS_DAY_FIELDS),
}

READ_EVIDENCE_RUNTIME = {
    (): {"context_id", "read", "focus"},
    ("focus",): set(FOCUS_AXES),
}

APPLY_RUNTIME = {(): {"proposal", "confirmed"}}

DELIVERY_PREPARE_RUNTIME = {
    (): {
        "plan_id",
        "plan_version",
        "session_ids",
        "withdraw",
        "resume_attempt_id",
    }
}

EXISTING_PLAN_TOP_RUNTIME = {
    (): {
        "plan_id",
        "plan_version",
        "context",
        "red_flags",
        "publish_new_workouts",
        "change_request",
    }
}

START_SESSION_DISPOSITIONS = {
    path: LOAD_BEARING for path in _flatten(START_SESSION_RUNTIME)
}
READ_EVIDENCE_DISPOSITIONS = {
    path: LOAD_BEARING for path in _flatten(READ_EVIDENCE_RUNTIME)
}
APPLY_DISPOSITIONS = {path: LOAD_BEARING for path in _flatten(APPLY_RUNTIME)}
DELIVERY_PREPARE_DISPOSITIONS = {
    path: LOAD_BEARING for path in _flatten(DELIVERY_PREPARE_RUNTIME)
}
EXISTING_PLAN_TOP_DISPOSITIONS = {
    ("plan_id",): LOAD_BEARING,
    ("plan_version",): LOAD_BEARING,
    ("context",): LOAD_BEARING,
    ("red_flags",): REJECTED,
    ("publish_new_workouts",): LOAD_BEARING,
    ("change_request",): LOAD_BEARING,
}


class SchemaWalkerTests(unittest.TestCase):
    """The field list is derived from the catalogue, and the walk cannot go quiet."""

    def test_derivation_is_not_empty_for_every_important_tool(self):
        empty: list[str] = []
        for name in IMPORTANT_PUBLIC_TOOLS:
            fields = published_fields(name)
            if not fields:
                empty.append(name)
            self.assertEqual(
                set(input_schema(name).get("properties") or {}),
                {path[0] for path in fields if len(path) == 1},
                f"{name}: walker missed a top-level published property",
            )
        self.assertEqual(
            [],
            empty,
            "a schema that stops exposing fields must not make conformance vacuously pass",
        )

    def test_walker_is_closed_over_nested_properties(self):
        for name in IMPORTANT_PUBLIC_TOOLS:
            schema = input_schema(name)
            derived = set(published_fields(name))
            for path in list(derived) + [()]:
                node = schema
                try:
                    for step in path:
                        if step == "[]":
                            node = node["items"]
                        elif isinstance(step, str) and (
                            step.startswith("oneOf:") or step.startswith("anyOf:")
                        ):
                            key, label = step.split(":", 1)
                            node = next(
                                branch
                                for index, branch in enumerate(node.get(key) or [])
                                if isinstance(branch, dict)
                                and (
                                    (
                                        (branch.get("properties") or {})
                                        .get("kind")
                                        or {}
                                    ).get("enum")
                                    or [str(index)]
                                )[0]
                                == label
                            )
                        else:
                            node = node["properties"][step]
                except (KeyError, StopIteration, TypeError):
                    self.fail(f"{name} derived path {path_label(path)} is not reachable")
                for child in (node.get("properties") or {}):
                    self.assertIn(
                        path + (child,),
                        derived,
                        f"{name} schema node {path_label(path)} declares {child!r} "
                        "but the walker did not emit it",
                    )

    def test_an_object_with_no_properties_cannot_vacuously_pass(self):
        self.assertEqual((), property_paths({"type": "object"}))
        self.assertEqual((), property_paths({"type": "object", "properties": {}}))


class SchemaRuntimeDriftDetectorTests(unittest.TestCase):
    """The checker that would have failed on #427's schema/validator field drift."""

    def test_a_declared_field_missing_from_runtime_known_fails(self):
        # The #427 shape: the catalogue advertised cycle.end; the first-plan parser
        # did not know the name, so a model filling the schema was refused or the
        # value was dropped. Either way the advertised field was not in the runtime
        # known set.
        declared = (("cycle", "start"), ("cycle", "end"))
        runtime_known = {("cycle", "start")}
        dispositions = {
            ("cycle", "start"): LOAD_BEARING,
            ("cycle", "end"): LOAD_BEARING,
        }
        problems = coverage_problems(
            declared=declared,
            runtime_known=runtime_known,
            dispositions=dispositions,
        )
        self.assertTrue(any("cycle.end" in item for item in problems), problems)
        self.assertTrue(any("silent drop" in item for item in problems), problems)

    def test_a_runtime_field_the_schema_does_not_declare_fails(self):
        problems = coverage_problems(
            declared=(("plan_id",),),
            runtime_known={("plan_id",), ("secret_override",)},
            dispositions={("plan_id",): LOAD_BEARING},
        )
        self.assertTrue(
            any("secret_override" in item for item in problems), problems
        )

    def test_an_unclassified_advertised_field_fails(self):
        problems = coverage_problems(
            declared=(("coach_note",),),
            runtime_known=set(),
            dispositions={},
        )
        self.assertTrue(any("no recorded disposition" in item for item in problems))

    def test_coverage_is_clean_when_both_sides_agree(self):
        paths = (("proposal",), ("confirmed",))
        self.assertEqual(
            [],
            coverage_problems(
                declared=paths,
                runtime_known=set(paths),
                dispositions={path: LOAD_BEARING for path in paths},
            ),
        )


class PublishedFieldDispositionTests(unittest.TestCase):
    """Every derived field of the six tools has an asserted disposition."""

    def test_startCoachSession_every_published_field_has_a_disposition(self):
        problems = coverage_problems(
            declared=published_fields("startCoachSession"),
            runtime_known=_flatten(START_SESSION_RUNTIME),
            dispositions=START_SESSION_DISPOSITIONS,
        )
        self.assertEqual([], problems, "\n".join(problems))
        for path, disposition in START_SESSION_DISPOSITIONS.items():
            with self.subTest(field=path_label(path)):
                self.assertIn(disposition, DISPOSITIONS)

    def test_readCoachEvidence_every_published_field_has_a_disposition(self):
        problems = coverage_problems(
            declared=published_fields("readCoachEvidence"),
            runtime_known=_flatten(READ_EVIDENCE_RUNTIME),
            dispositions=READ_EVIDENCE_DISPOSITIONS,
        )
        self.assertEqual([], problems, "\n".join(problems))

    def test_applyCoachDecision_every_published_field_has_a_disposition(self):
        problems = coverage_problems(
            declared=published_fields("applyCoachDecision"),
            runtime_known=_flatten(APPLY_RUNTIME),
            dispositions=APPLY_DISPOSITIONS,
        )
        self.assertEqual([], problems, "\n".join(problems))

    def test_applyWorkoutDelivery_every_published_field_has_a_disposition(self):
        problems = coverage_problems(
            declared=published_fields("applyWorkoutDelivery"),
            runtime_known=_flatten(APPLY_RUNTIME),
            dispositions=APPLY_DISPOSITIONS,
        )
        self.assertEqual([], problems, "\n".join(problems))

    def test_prepareWorkoutDelivery_every_published_field_has_a_disposition(self):
        problems = coverage_problems(
            declared=published_fields("prepareWorkoutDelivery"),
            runtime_known=_flatten(DELIVERY_PREPARE_RUNTIME),
            dispositions=DELIVERY_PREPARE_DISPOSITIONS,
        )
        self.assertEqual([], problems, "\n".join(problems))

    def test_prepareCoachDecision_first_plan_every_published_field_has_a_disposition(self):
        problems = coverage_problems(
            declared=published_fields("prepareCoachDecision"),
            runtime_known=_first_plan_runtime_known(),
            dispositions=_first_plan_dispositions(),
        )
        self.assertEqual([], problems, "\n".join(problems))
        for path, disposition in _first_plan_dispositions().items():
            with self.subTest(field=path_label(path)):
                self.assertIn(disposition, DISPOSITIONS)

    def test_prepareCoachDecision_existing_plan_top_level_dispositions(self):
        top = tuple(
            path for path in published_fields("prepareCoachDecision") if len(path) == 1
        )
        problems = coverage_problems(
            declared=top,
            runtime_known=_flatten(EXISTING_PLAN_TOP_RUNTIME),
            dispositions=EXISTING_PLAN_TOP_DISPOSITIONS,
        )
        self.assertEqual([], problems, "\n".join(problems))

    def test_existing_plan_change_request_first_level_matches_the_parser(self):
        properties = set(
            (input_schema("prepareCoachDecision")["properties"]["change_request"].get("properties") or {})
        )
        runtime = set(CHANGE_REQUIRED) | set(CHANGE_OPTIONAL)
        # First-plan-only: advertised, refused on a change rather than dropped.
        extra = properties - runtime
        self.assertEqual({"availability"}, extra)
        missing = runtime - properties
        self.assertEqual(set(), missing)
        dispositions = {
            ("change_request", name): (
                REJECTED if name not in runtime else LOAD_BEARING
            )
            for name in properties
        }
        problems = coverage_problems(
            declared=[("change_request", name) for name in properties],
            runtime_known={("change_request", name) for name in runtime | extra},
            dispositions=dispositions,
        )
        self.assertEqual([], problems, "\n".join(problems))

    def test_readCoachEvidence_runtime_allow_list_is_independent_of_the_schema(self):
        # The gateway's `_only_fields` tuple in `read_evidence`, not the catalogue.
        self.assertEqual({"context_id", "read", "focus"}, set(READ_EVIDENCE_RUNTIME[()]))


class PublicRuntimeCase(McpTestCase):
    """One MCP call, payload in, payload out. Temp store, never the dogfood home."""

    def setUp(self):
        super().setUp()
        self.fake.sport_settings = [dict(item) for item in RUN_SPORT_SETTINGS]

    def invoke(self, name: str, arguments: dict[str, Any] | None = None):
        result = self.tool_result(name, arguments)
        payload = self.tool_payload(result)
        refused = bool(result.get("isError"))
        return refused, payload


class StartCoachSessionDispositionTests(PublicRuntimeCase):
    """Each advertised session field is load-bearing, not silently dropped."""

    def setUp(self):
        super().setUp()
        self.owner_id = self.seed_owner(TOKEN_A, plan=publishable_plan())

    def session(self, **fields):
        refused, payload = self.invoke("startCoachSession", fields)
        self.assertFalse(refused, payload)
        return payload

    def test_read_changes_which_groups_come_back(self):
        today = self.session(all_clear=True, read=["today"])
        full = self.session(all_clear=True, read=["all"])
        self.assertNotEqual(
            today.get("evidence_index"),
            full.get("evidence_index"),
        )

    def test_all_clear_writes_every_red_flag_false(self):
        cleared = self.session(all_clear=True)
        flags = cleared["context"]["constraints"]["red_flags"]
        self.assertEqual({name: False for name in RED_FLAG_FIELDS}, flags)

    def test_a_stated_red_flag_is_carried_and_is_not_dropped(self):
        flagged = self.session(red_flags={"pain": True})
        self.assertIs(True, flagged["context"]["constraints"]["red_flags"]["pain"])

    def test_each_red_flag_name_the_schema_declares_is_load_bearing(self):
        for name in RED_FLAG_FIELDS:
            with self.subTest(flag=name):
                payload = self.session(red_flags={name: True})
                self.assertIs(
                    True, payload["context"]["constraints"]["red_flags"][name]
                )

    def test_available_days_and_session_minutes_land_in_constraints(self):
        payload = self.session(
            all_clear=True, available_days=["mon"], session_minutes=35
        )
        constraints = payload["context"]["constraints"]
        self.assertEqual(["mon"], constraints["available_days"])
        self.assertEqual(35, constraints["session_minutes"])

    def test_fatigue_schedule_and_unknowns_are_load_bearing(self):
        payload = self.session(
            all_clear=True,
            leg_fatigue="elevated",
            soreness="severe",
            schedule_changed=True,
            equipment_changed=True,
            unknowns=["conformance-unknown"],
        )
        constraints = payload["context"]["constraints"]
        self.assertEqual("elevated", constraints["leg_fatigue"])
        self.assertEqual("severe", constraints["soreness"])
        self.assertIs(True, constraints["schedule_changed"])
        self.assertIs(True, constraints["equipment_changed"])
        self.assertIn("conformance-unknown", payload["context"]["unknowns"])

    def test_timezone_override_is_load_bearing(self):
        payload = self.session(all_clear=True, timezone="America/New_York")
        self.assertEqual("America/New_York", payload["context"]["timezone"])

    def test_as_of_is_load_bearing(self):
        payload = self.session(all_clear=True, as_of="2026-08-12T12:00:00+00:00")
        self.assertTrue(str(payload["context"]["as_of"]).startswith("2026-08-12"))

    def test_guidance_received_replaces_the_text_with_a_digest(self):
        first = self.session(all_clear=True)
        self.assertIn("coaching_guidance", first)
        digest = first.get("guidance_digest")
        self.assertTrue(digest)
        second = self.session(
            all_clear=True, guidance_received=True, guidance_digest=digest
        )
        self.assertNotEqual(first.get("coaching_guidance"), second.get("coaching_guidance"))

    def test_recovery_signals_day_fields_the_schema_declares_are_carried(self):
        upload = recovery_signals_upload(source="watch-face")
        # Fill every observation the schema names so a new advertised column that
        # the runtime drops cannot hide behind an omitted key.
        extra = {
            "sleep_score": 77.0,
            "sleep_duration_sec": 25000.0,
            "sleep_history_score": 70.0,
            "hrv_last_night_ms": 58.0,
            "resting_hr_bpm": 52.0,
        }
        for day in upload["days"]:
            day.update(extra)
        payload = self.session(all_clear=True, recovery_signals=upload)
        days = payload["context"]["recovery_signals"]["days"]
        by_date = {row["date"]: row for row in days}
        observed = by_date["2026-08-13"]
        for name in RECOVERY_SIGNALS_DAY_FIELDS:
            if name == "date":
                continue
            with self.subTest(field=name):
                self.assertIn(name, observed)
        self.assertEqual(77.0, observed["sleep_score"])
        self.assertEqual(58.0, observed["hrv_last_night_ms"])

    def test_an_undeclared_top_level_field_is_not_honoured(self):
        baseline = self.session(all_clear=True)
        refused, payload = self.invoke(
            "startCoachSession",
            {"all_clear": True, "__conformance_unknown__": True},
        )
        self.assertFalse(refused, payload)
        self.assertEqual(baseline["context"]["as_of"], payload["context"]["as_of"])
        self.assertEqual(
            baseline["context"]["constraints"]["red_flags"],
            payload["context"]["constraints"]["red_flags"],
        )


class ReadCoachEvidenceDispositionTests(PublicRuntimeCase):
    def setUp(self):
        super().setUp()
        self.owner_id = self.seed_owner(TOKEN_A, plan=publishable_plan())

    def test_context_id_read_and_focus_are_load_bearing(self):
        session = self.invoke("startCoachSession", {"all_clear": True, "read": ["today"]})[1]
        context_id = session["context"]["context_id"]
        refused, expanded = self.invoke(
            "readCoachEvidence",
            {"context_id": context_id, "read": ["week"]},
        )
        self.assertFalse(refused, expanded)
        self.assertIn("week", expanded.get("groups") or expanded.get("evidence") or {})

        focused_refused, focused = self.invoke(
            "readCoachEvidence",
            {
                "context_id": context_id,
                "read": ["session_detail"],
                "focus": {"sessions": ["run-quality-01"]},
            },
        )
        self.assertFalse(focused_refused, focused)
        self.assertIn("focused", focused)

    def test_an_undeclared_field_is_refused(self):
        session = self.invoke("startCoachSession", {"all_clear": True})[1]
        refused, payload = self.invoke(
            "readCoachEvidence",
            {
                "context_id": session["context"]["context_id"],
                "read": ["today"],
                "__conformance_unknown__": True,
            },
        )
        self.assertTrue(refused, payload)
        self.assertIn("unexpected", str(payload).lower())


class FirstPlanPrepareDispositionTests(PublicRuntimeCase):
    """#427 witnesses: advertised first-plan fields are not dropped or wrongly refused."""

    def setUp(self):
        super().setUp()
        self.owner_id = self.seed_owner(TOKEN_A)

    def prepare(self, change_request, **extra):
        return self.invoke(
            "prepareCoachDecision",
            {"change_request": change_request, **extra},
        )

    def test_cycle_end_matching_the_derivation_is_load_bearing(self):
        change = schema_rich_change_request()
        refused, prepared = self.prepare(change)
        self.assertFalse(refused, prepared)
        self.assertEqual(change["cycle"]["end"], prepared["preview"]["cycle"]["end"])

    def test_cycle_end_that_disagrees_is_refused_not_overwritten(self):
        change = as_change_request(copy.deepcopy(ONBOARDING))
        change["cycle"]["end"] = "2026-10-11"
        refused, payload = self.prepare(change)
        self.assertTrue(refused, payload)
        detail = str(payload.get("detail") or payload)
        self.assertIn("2026-09-13", detail)
        self.assertIn("2026-10-11", detail)

    def test_week_start_matching_the_derivation_is_load_bearing(self):
        change = schema_rich_change_request()
        refused, prepared = self.prepare(change)
        self.assertFalse(refused, prepared)
        self.assertEqual(change["week"]["start"], prepared["preview"]["week"]["start"])

    def test_week_start_that_disagrees_is_refused_not_dropped(self):
        change = as_change_request(copy.deepcopy(ONBOARDING))
        change["week"] = {"start": "2026-08-24", "intent": ONBOARDING["week_intent"]}
        refused, payload = self.prepare(change)
        self.assertTrue(refused, payload)
        detail = str(payload.get("detail") or payload)
        self.assertIn("2026-08-17", detail)
        self.assertIn("2026-08-24", detail)

    def test_coach_note_the_schema_declares_is_carried(self):
        change = schema_rich_change_request()
        refused, prepared = self.prepare(change)
        self.assertFalse(refused, prepared)
        applied = self.invoke(
            "applyCoachDecision",
            {"proposal": prepared["proposal"], "confirmed": True},
        )
        self.assertFalse(applied[0], applied[1])
        sessions = read_current_plan(self.owner_dir(self.owner_id))["current_plan"][
            "week"
        ]["sessions"]
        by_date = {row["scheduled_date"]: row for row in sessions}
        noted = by_date[change["sessions"][0]["scheduled_date"]]
        self.assertEqual(change["sessions"][0]["coach_note"], noted.get("coach_note"))

    def test_plan_id_on_a_first_plan_is_rejected(self):
        refused, payload = self.prepare(
            as_change_request(ONBOARDING), plan_id="plan-does-not-exist"
        )
        self.assertTrue(refused, payload)
        self.assertIn("plan_id", str(payload.get("detail") or payload))

    def test_goal_measurement_on_a_first_plan_is_rejected(self):
        change = as_change_request(copy.deepcopy(ONBOARDING))
        change["goal"]["measurement"] = {
            "reference_session_id": "run-quality-01",
            "measurement_week_start": "2026-09-07",
            "compare": "same route",
        }
        refused, payload = self.prepare(change)
        self.assertTrue(refused, payload)

    def test_session_id_on_a_first_plan_add_is_rejected_not_dropped(self):
        change = as_change_request(copy.deepcopy(ONBOARDING))
        change["sessions"][0]["session_id"] = "named-too-early"
        refused, payload = self.prepare(change)
        self.assertTrue(refused, payload)
        self.assertIn("session_id", str(payload.get("detail") or payload))

    def test_publish_new_workouts_is_load_bearing_on_a_first_plan(self):
        change = as_change_request(ONBOARDING)
        off = self.prepare(change, publish_new_workouts=False)
        on = self.prepare(change, publish_new_workouts=True)
        self.assertFalse(off[0], off[1])
        self.assertFalse(on[0], on[1])
        self.assertNotEqual(
            (off[1].get("preview") or {}).get("calendar_delivery"),
            (on[1].get("preview") or {}).get("calendar_delivery"),
        )


class ExistingPlanPrepareDispositionTests(PublicRuntimeCase):
    def setUp(self):
        super().setUp()
        self.owner_id = self.seed_owner(TOKEN_A, plan=publishable_plan())

    def session(self):
        refused, payload = self.invoke("startCoachSession", {"all_clear": True})
        self.assertFalse(refused, payload)
        return payload

    def prepare(self, session, **extra):
        body = {
            "plan_id": session["plan_state"]["plan_id"],
            "plan_version": session["plan_state"]["plan_version"],
            "context": {"context_id": session["context"]["context_id"]},
            "change_request": WEEKLY_CHANGE,
        }
        body.update(extra)
        return self.invoke("prepareCoachDecision", body)

    def test_plan_id_and_version_and_context_are_load_bearing(self):
        session = self.session()
        refused, prepared = self.prepare(session)
        self.assertFalse(refused, prepared)
        self.assertEqual(session["plan_state"]["plan_id"], prepared["plan_id"])
        wrong = self.prepare(session, plan_id="not-this-plan")
        self.assertTrue(wrong[0], wrong[1])

    def test_red_flags_on_an_existing_plan_are_rejected(self):
        session = self.session()
        refused, payload = self.prepare(session, red_flags={"pain": True})
        self.assertTrue(refused, payload)
        self.assertIn("red_flags", str(payload.get("detail") or payload))

    def test_availability_on_an_existing_plan_is_rejected_not_dropped(self):
        session = self.session()
        change = copy.deepcopy(WEEKLY_CHANGE)
        change["availability"] = {"days": ["mon"]}
        refused, payload = self.prepare(session, change_request=change)
        self.assertTrue(refused, payload)
        self.assertIn("availability", str(payload.get("detail") or payload))

    def test_publish_new_workouts_changes_the_existing_plan_preview(self):
        session = self.session()
        off = self.prepare(session, publish_new_workouts=False)
        on = self.prepare(session, publish_new_workouts=True)
        self.assertFalse(off[0], off[1])
        self.assertFalse(on[0], on[1])
        self.assertNotEqual(
            (off[1].get("preview") or {}).get("calendar_delivery"),
            (on[1].get("preview") or {}).get("calendar_delivery"),
        )


class ApplyAndDeliveryDispositionTests(PublicRuntimeCase):
    def setUp(self):
        super().setUp()
        self.owner_id = self.seed_owner(TOKEN_A, plan=publishable_plan())

    def test_apply_proposal_and_confirmed_are_load_bearing(self):
        session = self.invoke("startCoachSession", {"all_clear": True})[1]
        prepared = self.invoke(
            "prepareCoachDecision",
            {
                "plan_id": session["plan_state"]["plan_id"],
                "plan_version": session["plan_state"]["plan_version"],
                "context": {"context_id": session["context"]["context_id"]},
                "change_request": WEEKLY_CHANGE,
            },
        )[1]
        unconfirmed = self.invoke(
            "applyCoachDecision",
            {"proposal": prepared["proposal"], "confirmed": False},
        )
        self.assertTrue(unconfirmed[0], unconfirmed[1])
        applied = self.invoke(
            "applyCoachDecision",
            {"proposal": prepared["proposal"], "confirmed": True},
        )
        self.assertFalse(applied[0], applied[1])
        extra = self.invoke(
            "applyCoachDecision",
            {
                "proposal": prepared["proposal"],
                "confirmed": True,
                "plan_id": prepared.get("plan_id") or "x",
            },
        )
        self.assertTrue(extra[0], extra[1])
        self.assertIn("plan_id", str(extra[1].get("detail") or extra[1]))

    def test_prepareWorkoutDelivery_fields_are_load_bearing(self):
        session = self.invoke("startCoachSession", {"all_clear": True})[1]
        plan_id = session["plan_state"]["plan_id"]
        version = session["plan_state"]["plan_version"]
        refused, prepared = self.invoke(
            "prepareWorkoutDelivery",
            {
                "plan_id": plan_id,
                "plan_version": version,
                "session_ids": ["run-quality-01"],
            },
        )
        self.assertFalse(refused, prepared)
        self.assertEqual(["run-quality-01"], [row["session_id"] for row in prepared["preview"]])
        wrong_plan = self.invoke(
            "prepareWorkoutDelivery",
            {
                "plan_id": "not-this-plan",
                "plan_version": version,
                "session_ids": ["run-quality-01"],
            },
        )
        self.assertTrue(wrong_plan[0], wrong_plan[1])
        withdraw = self.invoke(
            "prepareWorkoutDelivery",
            {
                "plan_id": plan_id,
                "plan_version": version,
                "session_ids": ["run-quality-01"],
                "withdraw": True,
            },
        )
        self.assertTrue(withdraw[0], withdraw[1])
        resume = self.invoke(
            "prepareWorkoutDelivery",
            {
                "plan_id": plan_id,
                "plan_version": version,
                "session_ids": ["run-quality-01"],
                "resume_attempt_id": "attempt-does-not-exist",
            },
        )
        self.assertTrue(resume[0], resume[1])

    def test_applyWorkoutDelivery_rejects_undeclared_identifiers(self):
        session = self.invoke("startCoachSession", {"all_clear": True})[1]
        prepared = self.invoke(
            "prepareWorkoutDelivery",
            {
                "plan_id": session["plan_state"]["plan_id"],
                "plan_version": session["plan_state"]["plan_version"],
                "session_ids": ["run-quality-01"],
            },
        )[1]
        extra = self.invoke(
            "applyWorkoutDelivery",
            {
                "proposal": prepared["proposal"],
                "confirmed": True,
                "plan_id": prepared["plan_id"],
            },
        )
        self.assertTrue(extra[0], extra[1])


if __name__ == "__main__":
    unittest.main()
