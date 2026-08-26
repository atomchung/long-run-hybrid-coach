"""What one ``startCoachSession`` read still hands the coach, held against a committed copy.

Every other test of this route asserts one property of one answer. This asserts that a
whole answer -- eighteen of them, one per scenario in
[coach_session_scenarios.py](coach_session_scenarios.py) -- is the same answer it was
when the snapshot was blessed. The committed file is the "before"; whatever this checkout
produces now is the "after". Nothing here needs a second git checkout, which is the whole
reason it exists: the comparison it replaces did, so it could not run in CI and could not
be repeated by the next person.

The failure it is built to catch is a read that quietly stops happening. That failure is
invisible from inside the code that caused it -- a field that used to carry the athlete's
lifts and now carries ``null`` looks, to the coach, exactly like an athlete who has not
lifted -- and it is what the last change to this read path could most easily have caused
while every existing test stayed green.

**Four axes, and all four are required.** They overlap, and that is deliberate: the
overlap is what makes a failure legible, because the axis that fails says what kind of
change it was before anyone opens the diff.

*Evidence* -- every context field a scenario populates is still populated, with the same
values. A field that goes from populated to ``null`` is the specific failure this exists
to catch, so it is asserted as its own thing rather than left to a whole-object compare
that would report it as one line among hundreds.

*Decision* -- ``validation``, ``reconciliation`` (including which sessions it applied,
which were ambiguous and which planned sessions went unmatched), ``delivery`` and the
plan version. The read is allowed to change what it *says*; it is not allowed to change
what it *did* to the plan without somebody deciding that.

*Continuity* -- what a second turn in the same conversation would still see: the plan it
is talking about, the id that ties this context to the instant it was built at, and the
span of cycle the review covers. A conversation that silently starts describing a
different window is a conversation whose second answer contradicts its first.

*Unknown handling* -- the ``unknowns`` list, exactly. AGENTS.md 3 makes this a
first-class output rather than a diagnostic: it is what the coach is told it does not
know, and an entry appearing or disappearing changes what the answer is allowed to claim.
So it fails until somebody re-blesses the snapshot, in both directions.

And beside the four, the provider request list per scenario, exactly. That is the
property the last change to this path bought, it is invisible in every response, and a
later refactor could give it back without a single field of any answer moving.

Re-blessing is ``python3 -m tests.coach_session_scenarios --write``, which every failure
message here names. It is a separate command on purpose: a test that refreshes its own
baseline on the way past cannot fail.
"""

from __future__ import annotations

import datetime as dt
import json
import unittest
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from garmin_coach_loop.orchestration import training_judgment

from tests import coach_session_scenarios as scenarios_module
from tests.coach_session_scenarios import (
    MANIFEST_NAME,
    REGENERATE_COMMAND,
    SNAPSHOTS,
    Scenario,
    manifest,
    scenarios,
)


ROOT = Path(__file__).resolve().parents[1]
CASES = ROOT / "evals" / "cases"

# The tail of every failure message here. A snapshot nobody can regenerate is deleted the
# first time it fails, so the way back travels with the failure rather than living in a
# file the person reading a red CI log is not looking at.
FIX = f"Re-bless with: {REGENERATE_COMMAND}, and read the diff before committing it."


# Eval cases whose declared ``evidence_fields`` no ``startCoachSession`` read in this file
# can supply, with the reason each one cannot be supplied.
#
# It is empty, and the emptiness is the finding rather than the absence of one. The
# throwaway comparison this file replaces bound 11 of the 18 cases and could bind neither
# the five ``plan_week`` cases nor the two ``plan_cycle`` ones, because none of its
# scenarios was a plan-authoring turn. That turned out to be a gap in the scenarios and
# not in the product: authoring a week or a cycle begins from the same read as every other
# turn, so what was missing was a scenario where the athlete had actually stated the
# things such a turn reads back -- the week they lose, the aim past this cycle, what they
# weigh, what they last lifted. ``12_plan_authoring__stated_evidence`` is that read, and
# with it every case binds.
#
# The constant stays because the number must not be able to drift in silence. A case that
# stops binding fails the test below and has to be entered here with a stated reason,
# which is a decision somebody makes rather than a count nobody notices changing.
UNBOUND_EVAL_CASES: dict[str, str] = {}


def _load_case(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _resolves(root: Any, path: str) -> bool:
    """Whether ``a.b[].c`` reaches a value in this *data*, hop by hop.

    ``[]`` maps over a list and every row has to resolve; an empty list does not, because
    a case declaring ``recovery_signals.days[].readiness_score`` cannot be answered from a
    read that returned no days. ``[*]`` asks for at least one row instead -- the quantifier
    for a field only some rows legitimately carry, like ``cycle_sessions[*].prescription``
    once rows before the previous week stopped carrying prose (issue #240 §3): the
    current and previous weeks' rows hold it, and a case reading those prescriptions is
    answerable from them.

    A ``null`` at the leaf does resolve, and that is a deliberate reading of AGENTS.md 3
    rather than a loophole. ``null`` here is a value with meaning -- "no measurement was
    scheduled", "no symptom was stated", "nothing has been delivered for this session" --
    and it is exactly what several of these cases exist to test the answer's handling of.
    What does not resolve is a container that is not there at all, which is the real
    failure: a case naming evidence no read of this product ever surfaces.

    ``test_evals.py`` already checks these same paths against ``contracts/``. That says
    the field is still in the schema; this says a real read still produces the shape.
    """
    return _resolve(root, path.split("."))


def _resolve(value: Any, segments: list[str]) -> bool:
    if not segments:
        return True
    segment, rest = segments[0], segments[1:]
    name, _, brackets = segment.partition("[")
    if not isinstance(value, dict) or name not in value:
        return False
    current = value[name]
    if not brackets.count("]"):
        return _resolve(current, rest)
    if not isinstance(current, list) or not current:
        return False
    if brackets.startswith("*"):
        return any(_resolve(item, rest) for item in current)
    return all(_resolve(item, rest) for item in current)


def _evidence_root(response: dict[str, Any] | None) -> dict[str, Any] | None:
    """The object an eval case's ``evidence_fields`` are read against.

    A case path is either a context path or a ``plan.``-prefixed PlanState path -- the
    same split ``test_evals.py`` resolves against two schemas -- so the two are joined
    here into the one object a coach actually has in hand after the read.
    """
    if response is None or response.get("context") is None:
        return None
    root = dict(response["context"])
    root["plan"] = (response.get("plan_state") or {}).get("current_plan")
    return root


def _populated(value: Any) -> bool:
    """Whether this field said anything. ``null`` and empty containers did not."""
    return value is not None and value != [] and value != {}


class CoachSessionScenarioTests(unittest.TestCase):
    """The committed reads, re-run and compared.

    Every scenario is run once for the whole class. They share nothing -- each builds and
    discards its own temporary store -- so running them once and asserting many times
    costs one run instead of one per axis, and no assertion can affect another's input.
    """

    declared: list[Scenario]
    live: dict[str, dict[str, Any]]
    stored: dict[str, dict[str, Any]]
    guidance: dict[str, str | None]

    @classmethod
    def setUpClass(cls) -> None:
        cls.declared = scenarios()
        cls.live = {}
        cls.guidance = {}
        cls.stored = {}
        for scenario in cls.declared:
            response, raised, requests = scenarios_module.run_response(scenario)
            cls.live[scenario.name] = scenarios_module.snapshot(
                scenario, response, raised, requests
            )
            cls.guidance[scenario.name] = (
                None if response is None else response.get("coaching_guidance")
            )
            cls.stored[scenario.name] = scenarios_module.load_snapshot(scenario.name)

    def each(self):
        """Every scenario with its committed snapshot and this checkout's answer."""
        for scenario in self.declared:
            yield scenario, self.stored[scenario.name], self.live[scenario.name]

    # -- the four axes ---------------------------------------------------------------

    def test_every_context_field_that_was_populated_still_is(self):
        """The evidence axis: nothing the coach was handed has gone quiet.

        Checked field by field rather than as one object compare, because the two
        failures read completely differently. A value that changed is a fixture or a
        calculation moving. A field that was populated and is now ``null`` is a read that
        stopped happening, and the coach cannot tell that from an athlete who did
        nothing.
        """
        for scenario, stored, live in self.each():
            with self.subTest(scenario=scenario.name):
                before = (stored["response"] or {}).get("context") or {}
                after = ((live["response"] or {}).get("context")) or {}
                emptied = sorted(
                    field
                    for field, value in before.items()
                    if _populated(value) and not _populated(after.get(field))
                )
                self.assertEqual(
                    [], emptied,
                    f"{scenario.name}: these context fields carried evidence when the "
                    f"snapshot was taken and carry none now. A read stopped happening, "
                    f"or stopped being reported. {FIX}",
                )
                changed = sorted(
                    field for field, value in before.items() if after.get(field) != value
                )
                self.assertEqual(
                    [], changed,
                    f"{scenario.name}: these context fields still carry evidence but no "
                    f"longer the same evidence. {FIX}",
                )
                self.assertEqual(
                    sorted(before), sorted(after),
                    f"{scenario.name}: the context gained or lost a field. A new field is "
                    f"paid for out of the budget in test_context_budget.py. {FIX}",
                )

    def test_the_decision_the_read_made_is_unchanged(self):
        """The decision axis: what the read did, not what it says.

        ``reconciliation`` is spelled out into its three lists rather than compared whole,
        because "applied one session" turning into "found it ambiguous" is a different
        product than "applied one session" turning into "applied a different one", and a
        single object compare reports both as one line.
        """
        for scenario, stored, live in self.each():
            with self.subTest(scenario=scenario.name):
                before = stored["response"] or {}
                after = live["response"] or {}
                self.assertEqual(
                    before.get("status"), after.get("status"),
                    f"{scenario.name}: the read's own status changed. {FIX}",
                )
                self.assertEqual(
                    before.get("validation"), after.get("validation"),
                    f"{scenario.name}: validation reached a different verdict. {FIX}",
                )
                before_reconciliation = before.get("reconciliation") or {}
                after_reconciliation = after.get("reconciliation") or {}
                for field in ("status", "applied", "ambiguous", "unmatched_planned"):
                    self.assertEqual(
                        before_reconciliation.get(field),
                        after_reconciliation.get(field),
                        f"{scenario.name}: reconciliation.{field} changed -- the read "
                        f"treated the athlete's actuals differently. {FIX}",
                    )
                self.assertEqual(
                    before.get("reconciliation"), after.get("reconciliation"),
                    f"{scenario.name}: reconciliation changed outside its three lists. {FIX}",
                )
                self.assertEqual(
                    before.get("delivery"), after.get("delivery"),
                    f"{scenario.name}: what the product claims to have delivered "
                    f"changed (AGENTS.md 8). {FIX}",
                )
                self.assertEqual(
                    (before.get("plan_state") or {}).get("plan_version"),
                    (after.get("plan_state") or {}).get("plan_version"),
                    f"{scenario.name}: the plan moved a different number of times. {FIX}",
                )

    def test_a_second_turn_in_the_same_conversation_would_see_the_same_thing(self):
        """The continuity axis.

        A coaching conversation is many turns against one plan, and the second turn is
        answered from whatever the first one established: which plan is being discussed,
        which instant the evidence was read at, and which stretch of the cycle the review
        covers. Each of those is pinned separately because each breaks the conversation in
        its own way -- a moved plan id makes the second answer about something else, a
        drifting context id detaches the answer from the moment it read, and a changed
        cycle span makes two turns describe different weeks as "this cycle".
        """
        for scenario, stored, live in self.each():
            with self.subTest(scenario=scenario.name):
                before = stored["response"] or {}
                after = live["response"] or {}
                for field in ("present", "plan_id", "plan_version"):
                    self.assertEqual(
                        (before.get("plan_state") or {}).get(field),
                        (after.get("plan_state") or {}).get(field),
                        f"{scenario.name}: plan_state.{field} changed, so a second turn "
                        f"would be about a different plan. {FIX}",
                    )
                self.assertEqual(
                    before.get("context_id"), after.get("context_id"),
                    f"{scenario.name}: context_id changed. {FIX}",
                )
                context = after.get("context")
                if context is not None:
                    self._assert_context_id_derives_from_the_pinned_instant(scenario, context)
                self.assertEqual(
                    self._cycle_span(before), self._cycle_span(after),
                    f"{scenario.name}: cycle_sessions covers a different span "
                    f"(first date, last date, count). {FIX}",
                )

    def _assert_context_id_derives_from_the_pinned_instant(
        self, scenario: Scenario, context: dict[str, Any]
    ) -> None:
        """``context_id`` is the read's instant, in the athlete's own day.

        Restated here rather than imported from the builder: the point is to notice the
        derivation changing, and a check that asks the code under test how it derives
        something agrees with it by construction. ``as_of`` is the pinned ``now`` in the
        athlete's timezone, so a context id that stops tracking it is a context id that no
        longer says which day the evidence came from.
        """
        as_of = dt.datetime.fromisoformat(context["as_of"])
        expected_as_of = scenario.now.astimezone(ZoneInfo(context["timezone"]))
        self.assertEqual(
            expected_as_of, as_of,
            f"{scenario.name}: as_of is no longer the instant this scenario pinned",
        )
        self.assertEqual(
            f"ctx-{as_of.strftime('%Y%m%d-%H%M%S')}", context["context_id"],
            f"{scenario.name}: context_id is no longer derived from as_of. {FIX}",
        )

    @staticmethod
    def _cycle_span(response: dict[str, Any]) -> tuple[Any, Any, int]:
        sessions = ((response.get("context") or {}).get("cycle_sessions")) or []
        dates = [session.get("date") for session in sessions]
        return (min(dates, default=None), max(dates, default=None), len(sessions))

    def test_the_unknowns_are_exactly_what_was_blessed(self):
        """The unknown-handling axis, in both directions.

        An entry that appears means the coach is now told it does not know something it
        used to be told; an entry that disappears means it is no longer told, which is the
        more dangerous of the two because a silently-dropped unknown reads as evidence.
        Neither is allowed to happen without somebody deciding it should.
        """
        for scenario, stored, live in self.each():
            with self.subTest(scenario=scenario.name):
                before = list((stored["response"] or {}).get("unknowns") or [])
                after = list((live["response"] or {}).get("unknowns") or [])
                self.assertEqual(
                    before, after,
                    f"{scenario.name}: what the coach is told it does not know has "
                    f"changed.\n  no longer stated: {sorted(set(before) - set(after))}"
                    f"\n  newly stated: {sorted(set(after) - set(before))}\n{FIX}",
                )

    # -- the cost of the read ----------------------------------------------------------

    def test_each_read_makes_exactly_the_provider_requests_it_was_blessed_with(self):
        """Exact, not an upper bound, and with the query window kept.

        Both failures this catches are silent in every response. A read that disappears
        shows up nowhere until the field it fed goes ``null`` months later; a read that is
        made twice inside one turn answers half the response from a different moment than
        the other half, and only when the athlete's account happens to move in between.
        The date window stays on each entry because a request that narrows its window is a
        lost read that an endpoint list alone reports as unchanged.
        """
        for scenario, stored, live in self.each():
            with self.subTest(scenario=scenario.name):
                self.assertEqual(
                    stored["provider_requests"], live["provider_requests"],
                    f"{scenario.name}: this read costs a different set of provider "
                    f"requests than it was blessed with. {FIX}",
                )

    # -- one invariant the axes above can only catch by accident ------------------------

    def test_a_pace_never_outlives_what_says_where_it_was_recorded(self):
        """Wherever a run's pace is, the fact qualifying it is beside it.

        A treadmill's distance is the machine's reading, so its pace is a different
        kind of number from a measured one and `recorded_indoors` is what says which.
        The trap is that the two live in different containers depending on how far the
        read is looking back: while a match is unsettled the pace is on the
        `recent_actuals` row, and once it settles that row is reduced to its
        reconciliation identity and the reading moves to the `cycle_sessions` record.
        A field added to one and not the other is invisible today and wrong in a
        review two weeks later -- and it was, until this test existed.

        Stated as its own invariant rather than left to the snapshot compare, which
        would report the same regression as one line among hundreds with nothing
        saying what it meant.
        """
        for scenario, _stored, live in self.each():
            with self.subTest(scenario=scenario.name):
                context = (live["response"] or {}).get("context") or {}
                for index, actual in enumerate(context.get("recent_actuals") or []):
                    if actual.get("sport") != "running":
                        continue
                    if actual.get("average_pace_sec_per_km") is None:
                        continue
                    self.assertIn(
                        "recorded_indoors", actual,
                        f"{scenario.name}: recent_actuals[{index}] carries a running "
                        f"pace with nothing saying where it was recorded. {FIX}",
                    )
                for index, record in enumerate(context.get("cycle_sessions") or []):
                    activity = record.get("activity")
                    if not isinstance(activity, dict) or record.get("sport") != "running":
                        continue
                    if activity.get("average_pace_sec_per_km") is None:
                        continue
                    self.assertIn(
                        "recorded_indoors", activity,
                        f"{scenario.name}: cycle_sessions[{index}].activity carries a "
                        f"running pace with nothing saying where it was recorded. {FIX}",
                    )

    # -- nothing outside the axes above may drift either --------------------------------

    def test_the_whole_snapshot_still_matches(self):
        """The catch-all, after the four axes have had their say.

        The axes above exist to make a failure legible, not to enumerate the response.
        Everything else the read returns -- the empty-account observations, the plan body
        a reconciling read hands back, the envelope -- is pinned here, so a change nobody
        wrote an axis for still has to be blessed.
        """
        for scenario, stored, live in self.each():
            with self.subTest(scenario=scenario.name):
                self.assertEqual(
                    stored, live,
                    f"{scenario.name}: the read differs from its committed snapshot "
                    f"outside the axes named above. {FIX}",
                )

    def test_the_training_judgment_still_rides_on_every_answer(self):
        """What the snapshot deliberately does not carry, asserted anyway.

        ``coaching_guidance`` is the same several thousand characters on every one of
        these answers, so committing eighteen copies would make one edit to that text an
        eighteen-file diff -- and hide whatever else moved in the same regeneration. It is
        left out of the files and checked here instead, because a response that stopped
        carrying it would be a coaching turn where the model never sees the training
        judgment at all, which is the failure that put the text in the response body in
        the first place.
        """
        judgment = training_judgment()
        for scenario in self.declared:
            carried = self.guidance[scenario.name]
            if carried is None:
                # The read that ends in a blocked build has no answer to carry anything.
                continue
            with self.subTest(scenario=scenario.name):
                self.assertIn(
                    judgment, carried,
                    f"{scenario.name}: the answer no longer carries the training judgment",
                )

    # -- the snapshot set itself --------------------------------------------------------

    def test_every_committed_snapshot_belongs_to_a_declared_scenario(self):
        """A file nobody runs is a file nobody notices is wrong."""
        declared = {f"{scenario.name}.json" for scenario in self.declared} | {MANIFEST_NAME}
        on_disk = {path.name for path in SNAPSHOTS.glob("*.json")}
        self.assertEqual(
            set(), on_disk - declared,
            f"these committed snapshots no longer belong to any scenario. {FIX}",
        )
        self.assertEqual(
            set(), declared - on_disk,
            f"these scenarios have no committed snapshot. {FIX}",
        )

    def test_the_manifest_says_what_each_scenario_is_for(self):
        stored = json.loads((SNAPSHOTS / MANIFEST_NAME).read_text(encoding="utf-8"))
        self.assertEqual(
            manifest(), stored,
            f"the manifest no longer describes the scenarios beside it. {FIX}",
        )
        self.assertFalse(stored["private_data"])

    def test_a_scenario_that_lost_its_snapshot_says_how_to_get_one(self):
        """The failure a missing file produces has to carry the way back with it.

        Without this the guard is untested in the one situation it exists for: the first
        run after somebody adds a scenario, or deletes a file they did not understand.
        """
        with self.assertRaises(AssertionError) as caught:
            scenarios_module.load_snapshot("no-such-scenario")
        self.assertIn(REGENERATE_COMMAND, str(caught.exception))


class DeclaredEvidenceBindingTests(unittest.TestCase):
    """Whether the cases in ``evals/cases`` name evidence a real read produces.

    ``evals/README.md`` keeps coaching judgment out of ``tests/`` and product mechanics
    out of ``evals/``, and this stays on the mechanical side of that line: it grades no
    answer and scores nothing. It asks one question about the cases -- can the evidence
    each one says a competent answer must read actually be read from a
    ``startCoachSession`` response? -- and that question is about this product's data
    shapes, which is what code and tests own.

    A case that fails it is not a wrong case. It is a case nobody can run, and the fix is
    usually a scenario that was never written rather than a case that was written wrong.
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.declared = scenarios()
        cls.roots: dict[str, tuple[tuple[str, ...], dict[str, Any] | None]] = {}
        for scenario in cls.declared:
            snapshot = scenarios_module.load_snapshot(scenario.name)
            cls.roots[scenario.name] = (
                scenario.modes,
                _evidence_root(snapshot["response"]),
            )
        cls.cases = {
            path.stem: _load_case(path) for path in sorted(CASES.glob("*.json"))
        }

    def _binding(self, case: dict[str, Any]) -> tuple[list[str], dict[str, list[str]]]:
        """Scenarios of this case's mode that supply all its evidence, and what the rest
        were missing."""
        satisfied: list[str] = []
        missing: dict[str, list[str]] = {}
        for name, (modes, root) in self.roots.items():
            if case["mode"] not in modes or root is None:
                continue
            absent = [path for path in case["evidence_fields"] if not _resolves(root, path)]
            if absent:
                missing[name] = absent
            else:
                satisfied.append(name)
        return satisfied, missing

    def test_every_case_is_answerable_from_a_read_of_its_own_mode(self):
        for case_id, case in self.cases.items():
            if case_id in UNBOUND_EVAL_CASES:
                continue
            with self.subTest(case=case_id):
                satisfied, missing = self._binding(case)
                self.assertTrue(
                    satisfied,
                    f"{case_id} declares evidence no scenario of mode {case['mode']!r} "
                    f"supplies: {missing or 'there is no scenario of that mode at all'}. "
                    f"Either a scenario is missing, or this case belongs in "
                    f"UNBOUND_EVAL_CASES with the reason it cannot be answered.",
                )

    def test_the_cases_nobody_can_answer_are_exactly_the_ones_recorded(self):
        """So the coverage number cannot drift in either direction.

        A case that starts binding has to be taken off the list, which is how the list
        stays a record of a real gap rather than a note somebody wrote once.
        """
        still_unbound = {
            case_id
            for case_id, case in self.cases.items()
            if not self._binding(case)[0]
        }
        self.assertEqual(
            set(UNBOUND_EVAL_CASES), still_unbound,
            "UNBOUND_EVAL_CASES no longer matches which cases a scenario can answer. "
            "Every entry needs a stated reason, and a case that now binds has to come off.",
        )

    def test_every_case_id_recorded_as_unbound_is_a_case_that_exists(self):
        self.assertEqual(
            set(), set(UNBOUND_EVAL_CASES) - set(self.cases),
            "UNBOUND_EVAL_CASES names a case that is not in evals/cases",
        )

    def test_the_resolver_can_actually_fail(self):
        """Without this, a resolver that returned True forever would report full coverage.

        The three shapes below are the three ways a declared path stops being answerable,
        and each has to be told apart from the ``null`` leaf on the last line, which is a
        real reading rather than a missing one.
        """
        root = {
            "goal_context": {"primary_goal": "x", "measurement": None},
            "recovery_signals": {"days": []},
            "cycle_sessions": [{"date": "2026-08-10"}],
        }
        self.assertFalse(_resolves(root, "goal_context.progress_score"))
        self.assertFalse(_resolves(root, "recovery_signals.days[].readiness_score"))
        self.assertFalse(_resolves(root, "cycle_sessions[].prescription"))
        self.assertTrue(_resolves(root, "goal_context.primary_goal"))
        self.assertTrue(_resolves(root, "cycle_sessions[].date"))
        self.assertTrue(_resolves(root, "goal_context.measurement"))

    def test_the_any_row_quantifier_is_any_and_still_fails(self):
        """``[*]`` must diverge from ``[]`` exactly where a field lives on some rows
        only -- the prose-window shape -- and still fail on a list where no row (or no
        list at all) carries it, or it would report every partial field as answerable
        forever."""
        root = {
            "cycle_sessions": [
                {"date": "2026-08-03"},
                {"date": "2026-08-10", "prescription": "Easy run 8公里"},
            ],
            "empty": [],
        }
        self.assertTrue(_resolves(root, "cycle_sessions[*].prescription"))
        self.assertFalse(_resolves(root, "cycle_sessions[].prescription"))
        self.assertFalse(_resolves(root, "cycle_sessions[*].purpose"))
        self.assertFalse(_resolves(root, "empty[*].prescription"))


if __name__ == "__main__":
    unittest.main()
