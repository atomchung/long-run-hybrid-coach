"""What a read hands the model, and what it must never quietly stop handing it.

The projection in `garmin_coach_loop/context_view.py` is the one place in this product
where evidence the build assembled does not reach the coach. That is worth having --
a heavy account builds 63,782 characters and a question about a stored goal reads almost
none of it -- and it is also exactly the mechanism that turns into "the coach never saw
the interval session" if nobody holds it to anything.

So three properties are held here rather than assumed:

1. **Nothing is dropped silently.** Every field the builder emits belongs to the core or
   to a group. A new field that belongs to neither fails this file, in the diff that adds
   it, rather than going missing from every read afterwards.
2. **The peak is bounded.** The default read and each single-purpose read are measured
   against the same heavy fixture the per-field budgets use, with a ceiling each. A
   ceiling is what these are for: the failure this mechanism exists to stop is one result
   too large for a client to accept, not a conversation that is large on average. Measured
   across seven blind coaching turns, the whole journey came out 1% *larger* than reading
   everything, because a turn that expands to every group pays the compact read as well.
3. **What was left out is reachable and identical.** Expanding a group returns the same
   rows the whole read would have carried, out of the same snapshot -- not a second read
   of a moved account.
"""

from __future__ import annotations

import json
import unittest

from garmin_coach_loop import context_view
from garmin_coach_loop.context_view import (
    ALL_GROUPS,
    CORE_FIELDS,
    DEFAULT_READ,
    EVIDENCE_GROUPS,
    EvidenceGroupError,
    evidence_index,
    group_slice,
    parse_read,
    project_context,
)
from tests.test_context_budget import _heavy_context


def _size(value: object) -> int:
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")))


# What each read costs on the heavy fixture, in characters of compact JSON. The whole
# context is 63,782 there, which is the number every line below is a saving against.
#
# Ceilings rather than measurements, set just above what the projection produces today,
# for the reason the per-field budgets are: a read that grows has to say what it bought.
# A group that gains a field moves exactly the reads that carry it, which is the change
# worth stopping in the diff that makes it.
READ_CEILINGS: dict[tuple[str, ...], int] = {
    DEFAULT_READ: 36_000,
    ("today",): 24_500,
    ("week",): 17_500,
    ("cycle",): 22_000,
    ("strength",): 15_000,
    ("session_detail",): 11_500,
    ("recovery",): 11_000,
    # Raised from 20,000 by issue #372's undeclared-measurement line: it lands in
    # `unknowns`, which is core, so it is paid by every read -- and the two widest reads
    # were already sitting closest to their ceilings.
    ("history",): 20_500,
    # The read a stored-record correction makes: the core alone, which already carries
    # what the athlete has stated they want and how they like to train. Raised with the
    # core's own ceiling when issue #372's undeclared-measurement line landed in
    # `unknowns`.
    (): 4_500,
}


class EveryFieldHasAHomeTests(unittest.TestCase):
    """The property that makes the rest of this safe to have at all."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.context = _heavy_context()

    def test_every_field_the_builder_emits_is_core_or_in_a_group(self):
        """A field in neither is a field no read ever returns.

        It would not fail anything else: the build still writes it, the store still
        holds it, `validate_bundle` still reads it, and the retained copy a confirmation
        names still carries it. The only thing that changes is that the coach stops
        seeing it, which is the failure mode this whole mechanism has.
        """
        placed = set(CORE_FIELDS)
        for fields in EVIDENCE_GROUPS.values():
            placed |= set(fields)

        self.assertEqual(
            set(), set(self.context) - placed,
            "these context fields belong to no read; add each to CORE_FIELDS or to the "
            "group whose question needs it",
        )

    def test_no_group_names_a_field_the_builder_cannot_emit(self):
        """The other direction: a group promising a field nothing writes indexes a lie."""
        emitted = set(self.context)
        for group, fields in EVIDENCE_GROUPS.items():
            with self.subTest(group=group):
                self.assertEqual(set(), set(fields) - emitted, group)

    def test_the_core_is_small_enough_to_be_in_every_read(self):
        """It is paid for on every turn, so it is the one part that cannot grow freely.

        Raised from 4,000 to 4,500 when issue #372's undeclared-measurement line landed
        in `unknowns`, which is core: the line is the point of that issue, and the core
        is where a fact every coaching turn needs belongs. The ceiling still bounds the
        shape -- a field large enough to matter belongs to a group, not here.
        """
        core, _ = project_context(self.context, ())

        self.assertLessEqual(
            _size(core), 4_500,
            "the always-loaded core has grown; a field that big belongs to a group",
        )


class WhatEachReadCostsTests(unittest.TestCase):
    """The budget gate, on the same fixture the per-field ceilings use."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.context = _heavy_context()

    def test_every_read_fits_its_ceiling(self):
        for groups, ceiling in READ_CEILINGS.items():
            with self.subTest(read=groups):
                view, _ = project_context(self.context, groups)
                self.assertLessEqual(_size(view), ceiling, f"{groups} is over budget")

    def test_the_default_read_is_materially_smaller_than_the_whole_build(self):
        """Materially, not marginally: the point is a turn that stops paying for months
        of history it never reads."""
        whole, index = project_context(self.context, ALL_GROUPS)
        default, _ = project_context(self.context, DEFAULT_READ)

        self.assertIsNone(index, "a read of everything has nothing to index")
        self.assertLess(_size(default) * 3, _size(whole) * 2)

    def test_a_stored_record_correction_reads_almost_nothing(self):
        """Issue #250's own example: correcting a goal is not a training question.

        What it needs is what the athlete has stated, which is in the core, and what
        they have measured. Six weeks of provider actuals are not evidence about their
        stated intention.
        """
        view, _ = project_context(self.context, ("records",))

        self.assertLess(_size(view) * 5, _size(self.context))


class TheIndexSaysWhatIsMissingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.context = _heavy_context()

    def test_every_group_left_out_is_named_with_what_it_holds(self):
        index = evidence_index(self.context, ("today",))

        self.assertEqual(["today"], index["loaded"])
        named = {row["group"] for row in index["not_loaded"]}
        self.assertEqual(set(ALL_GROUPS) - {"today"}, named)
        history = next(row for row in index["not_loaded"] if row["group"] == "history")
        held = {entry["field"] for entry in history["holds"]}
        self.assertIn("training_history", held)
        rows = next(entry for entry in history["holds"] if entry["field"] == "training_history")
        self.assertGreater(rows["rows"], 0)
        self.assertIn("..", rows["spans"])

    def test_a_group_with_nothing_on_record_is_named_as_holding_nothing(self):
        """"There is no strength evidence" and "it was not loaded" are different answers.

        A coach that cannot tell them apart asks the athlete for something the product
        already knows is not there.
        """
        empty = {key: value for key, value in self.context.items()}
        empty["strength_execution"] = None
        empty["movement_history"] = None
        empty["set_structure"] = None

        index = evidence_index(empty, ("today",))

        strength = next(row for row in index["not_loaded"] if row["group"] == "strength")
        self.assertEqual([], strength["holds"])

    def test_a_read_of_everything_has_no_index_at_all(self):
        self.assertIsNone(evidence_index(self.context, ALL_GROUPS))


class ExpandingReturnsTheSameEvidenceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.context = _heavy_context()

    def test_a_group_expanded_is_byte_identical_to_the_whole_read(self):
        """The property that makes a wrong first choice cost one call and nothing else."""
        whole, _ = project_context(self.context, ALL_GROUPS)
        fields, holds = group_slice(self.context, ("history", "strength"))

        for field, value in fields.items():
            with self.subTest(field=field):
                self.assertEqual(whole[field], value)
        # And the caller can still tell which group answers which question.
        self.assertEqual({"history", "strength"}, set(holds))
        for group, names in holds.items():
            self.assertEqual(set(names), set(EVIDENCE_GROUPS[group]) & set(fields))

    def test_a_field_two_groups_share_is_sent_once(self):
        """Measured before it was a rule: on one expansion naming six groups, half the
        response was a second copy of a field another group in the same response already
        carried."""
        fields, holds = group_slice(self.context, ("today", "recovery"))

        shared = set(EVIDENCE_GROUPS["today"]) & set(EVIDENCE_GROUPS["recovery"])
        self.assertTrue(shared, "these two groups are supposed to overlap")
        for field in shared:
            with self.subTest(field=field):
                self.assertIn(field, holds["today"])
                self.assertIn(field, holds["recovery"])
        self.assertEqual(len(fields), len(set(fields)))
        self.assertLess(
            _size(fields),
            sum(_size({f: self.context[f] for f in EVIDENCE_GROUPS[g] if f in self.context})
                for g in ("today", "recovery")),
        )

    def test_a_default_read_plus_its_expansions_is_the_whole_read(self):
        view, _ = project_context(self.context, DEFAULT_READ)
        rest, _ = group_slice(
            self.context, tuple(g for g in ALL_GROUPS if g not in DEFAULT_READ)
        )

        rebuilt = {**view, **rest}
        whole, _ = project_context(self.context, ALL_GROUPS)

        self.assertEqual(whole, rebuilt)


class WhatAReadPurposeIsNotTests(unittest.TestCase):
    def test_an_unknown_group_is_refused_by_name_with_what_it_takes(self):
        with self.assertRaises(EvidenceGroupError) as raised:
            parse_read(["yesterday"])

        self.assertIn("yesterday", str(raised.exception))
        self.assertIn("today", str(raised.exception))

    def test_omitting_it_reads_for_a_coaching_turn_rather_than_failing(self):
        """A client that has never heard of this still gets a coaching turn's evidence."""
        self.assertEqual(DEFAULT_READ, parse_read(None))

    def test_the_same_groups_in_any_order_are_the_same_read(self):
        self.assertEqual(parse_read(["week", "today"]), parse_read(["today", "week"]))

    def test_all_is_every_group(self):
        self.assertEqual(ALL_GROUPS, parse_read("all"))
        self.assertEqual(ALL_GROUPS, parse_read(["records", "all"]))

    def test_a_read_purpose_carries_no_authority(self):
        """It picks what comes back first. It is not a capability, and there is nothing
        in this module that could make it one -- no group grants an operation, and the
        expansion route returns evidence out of a snapshot rather than taking any."""
        self.assertNotIn("write", context_view.__dict__)
        for group in ALL_GROUPS:
            with self.subTest(group=group):
                self.assertTrue(set(EVIDENCE_GROUPS[group]))


if __name__ == "__main__":
    unittest.main()


class OneSessionOneDayOneMovementTests(unittest.TestCase):
    """The other half of issue #250: a question about one thing, answered completely.

    A group is the right unit for "what has this month looked like" and the wrong one for
    "how did Thursday's threshold run go". The second question is answerable from a
    handful of rows spread across five fields, and reading whole groups to reach them is
    what makes a narrow question cost 25,000 characters.

    The property that makes this safe is that it removes *rows*, never parts of one. What
    a session's evidence is made of -- the prescription it was given, what was executed,
    the window and baseline it is compared against, and the source of each -- is exactly
    what issue #249 proved cannot be trimmed field-by-field, so nothing here trims it.
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.context = _heavy_context()
        cls.whole, _ = group_slice(cls.context, ALL_GROUPS)

    def _focus(self, **axes: object):
        focus = context_view.parse_focus(dict(axes))
        return context_view.focus_slice(self.context, dict(self.whole), focus)

    def test_one_session_carries_its_prescription_its_actual_and_its_source(self):
        session = self.context["cycle_sessions"][0]
        narrowed, report = self._focus(sessions=[session["session_id"]])

        row = narrowed["cycle_sessions"][0]
        self.assertEqual(session, row, "a matched row is the row, not a summary of it")
        self.assertIn("prescription", row)
        self.assertIn("activity_evidence", row)
        self.assertEqual(1, report["kept"]["cycle_sessions"]["rows"])
        self.assertEqual(
            len(self.context["cycle_sessions"]), report["kept"]["cycle_sessions"]["of"]
        )
        self.assertNotIn("matched_nothing", report)

    def test_a_session_brings_the_day_it_was_trained_on_with_it(self):
        """The link that makes one call enough.

        Lifts, segments and recovery readings carry no session id -- they are keyed by
        the activity the provider recorded and the day it happened. Resolving one to the
        other here is the difference between a session's evidence and a session's row.
        """
        trained = next(
            row
            for row in self.context["recent_actuals"]
            if row.get("planned_session_id") and row.get("date")
        )
        narrowed, _ = self._focus(sessions=[trained["planned_session_id"]])

        self.assertIn(trained, narrowed["recent_actuals"])
        for field in ("strength_execution", "segment_execution", "run_drift"):
            for row in _rows(narrowed.get(field)):
                self.assertNotEqual(
                    [], [row], "a row that came back must have come back whole"
                )

    def test_one_day_is_every_field_that_records_that_day(self):
        day = self.context["recovery_signals"]["days"][0]["date"]
        narrowed, report = self._focus(dates=[day])

        self.assertEqual([day], [row["date"] for row in narrowed["recovery_signals"]["days"]])
        self.assertEqual(
            {row["date"] for row in narrowed["body_measurements"]["measurements"]},
            {day},
        )
        # The month that day sits in, because a month row's date is a prefix of it.
        self.assertTrue(
            all(
                day.startswith(row["month"])
                for row in narrowed["training_history"]["months"]
            )
        )
        self.assertTrue(report["kept"]["recovery_signals"]["of"] > 1)

    def test_one_movement_is_its_lifts_and_its_arithmetic_and_nothing_else(self):
        movement = self.context["movement_history"]["movements"][0]["exercise"]
        narrowed, report = self._focus(movements=[movement])

        self.assertEqual(
            [movement],
            [row["exercise"] for row in narrowed["movement_history"]["movements"]],
        )
        self.assertEqual(
            {movement},
            {row["exercise"] for row in narrowed["strength_execution"]["sessions"]},
        )
        # Every set of every kept lift, exactly as reported -- the one-copy rule the
        # per-field budgets describe is what makes this the only place they live.
        for row in narrowed["strength_execution"]["sessions"]:
            self.assertTrue(row["sets"])
            self.assertIn("source", row)
        self.assertIn("movement_history", report["kept"])

    def test_a_focus_is_smaller_than_the_groups_it_narrows(self):
        session = self.context["cycle_sessions"][0]["session_id"]
        narrowed, _ = self._focus(sessions=[session])

        self.assertLess(_size(narrowed) * 2, _size(self.whole))

    def test_what_a_focus_left_out_is_counted_rather_than_hidden(self):
        """A short answer must not be mistakable for thin evidence.

        This is the failure the whole mechanism could most easily become: a coach that
        asks about one session, sees one lift, and concludes the athlete barely trains.
        The count says the field held thirteen.
        """
        movement = self.context["movement_history"]["movements"][0]["exercise"]
        _, report = self._focus(movements=[movement])

        kept = report["kept"]["strength_execution"]
        self.assertLess(kept["rows"], kept["of"])
        self.assertEqual(
            len(self.context["strength_execution"]["sessions"]), kept["of"]
        )

    def test_a_focus_matching_nothing_says_so_instead_of_answering_empty(self):
        narrowed, report = self._focus(sessions=["no-such-session"])

        self.assertIn("matched_nothing", report)
        self.assertEqual({}, {k: v for k, v in narrowed.items() if k in {"cycle_sessions"}})

    def test_the_fields_a_comparison_is_against_are_never_narrowed(self):
        """Baselines and the goal are what a focused row is read against.

        They are one row per thing that is true rather than per thing that happened, so
        narrowing them would remove the comparison rather than the noise.
        """
        session = self.context["cycle_sessions"][0]["session_id"]
        narrowed, report = self._focus(sessions=[session])

        self.assertEqual(self.whole["baseline_evidence"], narrowed["baseline_evidence"])
        self.assertNotIn("baseline_evidence", report["kept"])

    def test_a_focus_naming_no_axis_or_an_unknown_one_is_refused_by_name(self):
        with self.assertRaises(context_view.FocusError) as unknown:
            context_view.parse_focus({"weeks": ["2026-01-05"]})
        self.assertIn("sessions, dates, movements", str(unknown.exception))

        with self.assertRaises(context_view.FocusError):
            context_view.parse_focus({})
        with self.assertRaises(context_view.FocusError):
            context_view.parse_focus({"dates": [1]})

    def test_omitting_a_focus_reads_whole_groups(self):
        self.assertIsNone(context_view.parse_focus(None))


def _rows(value: object) -> list:
    if isinstance(value, list):
        return value
    if isinstance(value, dict):
        for item in value.values():
            if isinstance(item, list):
                return item
    return []


class AFocusNeverRemovesAFieldItDidNotFilterTests(unittest.TestCase):
    """The failure the focus mechanism could most easily hide.

    `training_history` holds two lists: months, which answer a date, and per-movement
    longevity, which answers a movement. Filtering one and dropping the other turns "this
    account has none" into "this field does not exist" -- and that distinction is the one
    `evidence_index` exists to preserve everywhere else in this module.
    """

    def test_a_second_list_that_is_absent_stays_absent_rather_than_vanishing(self):
        context = _heavy_context()
        context["training_history"] = {
            "source": "athlete_imported",
            "months": [{"month": "2026-01", "sport": "running", "session_count": 3}],
            "movement_longevity": None,
        }
        fields, _ = group_slice(context, ("history",))

        narrowed, _ = context_view.focus_slice(
            context, dict(fields), context_view.parse_focus({"dates": ["2026-01-08"]})
        )

        self.assertIn("movement_longevity", narrowed["training_history"])
        self.assertIsNone(narrowed["training_history"]["movement_longevity"])
        self.assertEqual("athlete_imported", narrowed["training_history"]["source"])

    def test_a_movement_focus_keeps_the_longevity_row_and_drops_the_months(self):
        context = _heavy_context()
        movement = context["training_history"]["movement_longevity"][0]["exercise"]
        fields, _ = group_slice(context, ("history",))

        narrowed, report = context_view.focus_slice(
            context, dict(fields), context_view.parse_focus({"movements": [movement]})
        )

        self.assertEqual([], narrowed["training_history"]["months"])
        self.assertEqual(
            [movement],
            [
                row["exercise"]
                for row in narrowed["training_history"]["movement_longevity"]
            ],
        )
        # Counted across both lists, so the report says how much of the field was passed
        # over rather than only how much of one of its halves.
        kept = report["kept"]["training_history"]
        self.assertLess(kept["rows"], kept["of"])


class AMovementIsFoundByTheDayItWasLiftedTests(unittest.TestCase):
    """A movement row carries no date of its own; its dates are one level down.

    Without reaching into `occurrences`, a focus on a day or a session dropped
    `movement_history` entirely -- and with it the baseline, the prescribed sets and the
    per-load arithmetic, which is precisely the comparison a focused answer promises to
    keep. `strength_execution` alone is raw sets with nothing to read them against.
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.context = _heavy_context()

    def test_a_day_focus_keeps_the_movements_lifted_that_day_with_their_baselines(self):
        day = self.context["strength_execution"]["sessions"][0]["date"]
        fields, _ = group_slice(self.context, ("strength",))

        narrowed, report = context_view.focus_slice(
            self.context, dict(fields), context_view.parse_focus({"dates": [day]})
        )

        self.assertIn("movement_history", narrowed)
        movements = narrowed["movement_history"]["movements"]
        self.assertTrue(movements)
        for row in movements:
            self.assertTrue(
                any(item.get("date") == day for item in row["occurrences"]),
                row["exercise"],
            )
            # Whole rows: the baseline and every occurrence, not the day's slice of them.
            self.assertIn("baseline", row)
            self.assertIn("occurrences", row)
        self.assertEqual(
            len(movements), report["kept"]["movement_history"]["rows"]
        )

    def test_one_movement_is_kept_and_another_is_not_in_the_same_call(self):
        """A mixed outcome, because a negative alone proves nothing here.

        Before `nested_dates`, `movement_history` had no date axis at all -- so *every*
        date focus excluded every row, and "nothing matched this day" was true whether
        or not the mechanism existed. Only a day that keeps one movement and drops
        another can tell the two apart.
        """
        movements = self.context["movement_history"]["movements"]
        self.assertGreater(len(movements), 1, "this test needs two movements")
        kept_day = movements[0]["occurrences"][0]["date"]
        other = movements[1]
        self.assertFalse(
            any(item["date"] == kept_day for item in other["occurrences"]),
            "the fixture's two movements now share a day; pick another",
        )
        fields, _ = group_slice(self.context, ("strength",))

        narrowed, report = context_view.focus_slice(
            self.context, dict(fields), context_view.parse_focus({"dates": [kept_day]})
        )

        returned = [row["exercise"] for row in narrowed["movement_history"]["movements"]]
        self.assertIn(movements[0]["exercise"], returned)
        self.assertNotIn(other["exercise"], returned)
        kept = report["kept"]["movement_history"]
        self.assertEqual(len(returned), kept["rows"])
        self.assertEqual(len(movements), kept["of"])

    def test_a_day_no_movement_was_lifted_on_keeps_none_and_says_how_many(self):
        fields, _ = group_slice(self.context, ("strength",))

        _, report = context_view.focus_slice(
            self.context, dict(fields), context_view.parse_focus({"dates": ["1999-01-01"]})
        )

        kept = report["kept"]["movement_history"]
        self.assertEqual(0, kept["rows"])
        self.assertEqual(len(self.context["movement_history"]["movements"]), kept["of"])


class EverySpecMatchesTheShapeTheBuilderEmitsTests(unittest.TestCase):
    """The failure mode `_ROWS` has and nothing else in this module does.

    A spec is a hand-written description of another module's output. When the two drift,
    nothing raises: a renamed container makes the field come back *whole* and a renamed
    date key makes it come back *empty*, and both look like an ordinary answer. So the
    spec is checked against what the builder actually produces.
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.context = _heavy_context()

    def test_every_container_and_axis_key_exists_in_the_real_shape(self):
        for field, spec in context_view._ROWS.items():
            value = self.context.get(field)
            if value is None:
                continue  # Null on this fixture says nothing about the spec.
            with self.subTest(field=field):
                container = spec.get("container")
                rows = value if container is None else value.get(container)
                self.assertIsInstance(
                    rows, list, f"{field}: no list at {container!r}"
                )
                if not rows:
                    continue
                keys = set(rows[0])
                for axis in ("sessions", "dates", "movements"):
                    for name in spec.get(axis, ()):
                        self.assertIn(name, keys, f"{field}.{name} ({axis})")
                nested = spec.get("nested_dates")
                if nested:
                    inner, key = nested
                    self.assertIn(inner, keys, f"{field}.{inner}")
                    for row in rows:
                        for item in row.get(inner) or []:
                            self.assertIn(key, item, f"{field}.{inner}[].{key}")
                            break
