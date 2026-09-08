"""What a read hands the model, and what it must never quietly stop handing it.

The projection in `garmin_coach_loop/context_view.py` is the one place in this product
where evidence the build assembled does not reach the coach. That is worth having --
a heavy account builds 63,508 characters and a question about a stored goal reads almost
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
# context is 63,508 there, which is the number every line below is a saving against.
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
    ("history",): 20_000,
    ("records",): 11_500,
    # The read a stored-record correction makes: the core alone, which already carries
    # what the athlete has stated they want and how they like to train.
    (): 4_000,
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
        """It is paid for on every turn, so it is the one part that cannot grow freely."""
        core, _ = project_context(self.context, ())

        self.assertLessEqual(
            _size(core), 4_000,
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
