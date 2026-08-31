"""What the A/B harness has to be right about before any answer it records means anything.

Three groups, and they fail for three different reasons:

* the **suite** names reads and sessions that exist, and covers the questions it claims
  to cover. A turn pointing at a scenario nobody kept is a turn that silently stopped
  asking anything;
* the **arms** are still what they were captured as. A frozen arm is this checkout's
  answer with two fields swapped, which is only an honest reconstruction while nothing
  else about the answer has moved -- so the digest recorded beside each overlay is
  checked here, and a build that changes a third field turns into a red test rather than
  into a quietly wrong comparison;
* the **figure check** finds a stated number the context never carried, and does not find
  one it did. Both halves: a check that fires on every correctly-cited pace is a check
  nobody reads.

Nothing here calls a model, and nothing here writes inside the repository.
"""

from __future__ import annotations

import contextlib
import copy
import io
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from evals.ab import figures
from evals.ab import harness
from garmin_coach_loop.validation import validate_coach_context
from tests import coach_session_scenarios as scenarios_module


def _with_packet_echo(answer: str, packet_id: str) -> str:
    """An answer shaped the way every packet ``create_run`` builds today requires.

    Every packet carries ``harness.PACKET_ECHO_INSTRUCTION``, so a bare answer with
    nothing appended is refused from here on -- see ``PacketEchoTests`` below.
    ``record_response`` strips exactly this suffix back off before storing the answer,
    so a test asserting the stored or reported text still compares against the
    original, unechoed string.
    """
    return f"{answer}\npacket: {packet_id}"


# The seven reads the suite exists to cover. Named here rather than counted from the
# suite, so deleting a turn shows up as a missing area instead of as a smaller list.
REQUIRED_COVERAGE = {
    "fourth_week_reads_week_one_quality",
    "previous_week_review",
    "measurement_reference_in_a_past_week",
    "probable_match",
    "same_day_unattached_session",
    "strength_alias",
    "today",
    "week_review",
    "cycle_review",
}


class OverlayValidityTests(unittest.TestCase):
    """Every frozen arm has to be a context the product could actually have built.

    An overlay is hand-written or captured from an older commit, and neither route
    checks it against the contract. So a field the reader later stops carrying stays in
    the overlay, `additionalProperties: false` never sees it, and the arm quietly serves
    the coach a field that does not exist -- which is worse than a broken arm, because
    the comparison still runs and the answer cites the phantom field as evidence.

    This happened: `run_drift` briefly carried `step_length_mm`, the reader dropped it
    once pace and cadence were shown to state it, and the overlay kept it. One eval
    answer read it back as fact before this test existed.

    Only extra fields are checked, never missing ones -- see the comment on the
    assertion for why a missing field is usually the arm's whole purpose.
    """

    def test_no_frozen_arm_overlays_a_field_the_contract_does_not_have(self):
        for suite_path in sorted(Path("evals/ab").glob("suite*.json")) + sorted(
            Path("evals/ab/suites").glob("*.json")
        ):
            suite = json.loads(suite_path.read_text(encoding="utf-8"))
            for arm in suite.get("arms", []):
                if arm.get("source") != "frozen":
                    continue
                for overlay_file in sorted(
                    (Path("evals/ab/arms") / arm["arm_id"]).glob("*.json")
                ):
                    record = json.loads(overlay_file.read_text(encoding="utf-8"))
                    with self.subTest(arm=arm["arm_id"], scenario=record["scenario"]):
                        context = dict(
                            harness._scenario_response(record["scenario"])["context"]
                        )
                        context.update(record["overlay"])
                        report = validate_coach_context(context)
                        # Only fields the contract does not have. A *missing* field is
                        # frequently the whole point -- `prose-window-two-weeks` exists
                        # to be a build that dropped a prescription -- so demanding a
                        # fully valid context would refuse the arms this suite needs.
                        # An extra field is never deliberate: nothing serves it, so the
                        # coach reads a value no build could produce.
                        # An arm may deliberately propose a shape the product does not
                        # have yet -- that is what a hypothetical arm is for -- but it
                        # has to say so, by name, in `proposes_fields`. Undeclared is
                        # the failure this test exists for: a field nothing serves,
                        # left behind by a reader that stopped carrying it.
                        proposed = set(record.get("proposes_fields") or ())
                        unknown = [
                            error for error in report.get("errors", [])
                            if "is not allowed" in error
                            and not any(f"context.{name}" in error for name in proposed)
                        ]
                        self.assertEqual(
                            [], unknown,
                            f"{arm['arm_id']}/{record['scenario']} overlays a field the "
                            f"contract does not have",
                        )


class OverlayArithmeticTests(unittest.TestCase):
    """A frozen arm may not state something the read it overlays makes impossible.

    `run_drift` reports a run's first third against its last, and `recent_actuals`
    reports the same run's average. An average lies between its parts, so an overlay
    whose two ends sit on the same side of it is describing a run that cannot exist.

    This happened, and it is why the check is here rather than in the contract: the
    fixture was reshaped from the athlete's real runs and carried their absolute paces
    onto sessions with different averages. Both ends of all three runs landed slower
    than the average they were supposed to bracket. Two eval samples noticed and said
    so -- "分段配速跟整趟平均對不起來，所以我只讀方向" -- which means a fixture bug was
    silently costing the arm the evidence it exists to supply, in the only place nobody
    was checking.

    `set_structure` carries the same exposure for a lift, in two places. Within one
    entry, `under_load_sec` sums a session's own work sets and `recorded_sec` adds its
    rests on top of that (`fit_sets.summarise_sets`) -- a rest cannot subtract time, so
    the first can never be larger than the second. And across entries, `recorded_sec`
    is a second, independent reading of the same session `recent_actuals` reports as
    `duration_minutes`: one is parsed straight out of the FIT file's own SET messages,
    the other is Intervals' `moving_time` for the whole activity
    (`source_intervals._build_set_structure` / `_activity_candidates`). A lift's sets
    and their rests happen inside the span the activity was recorded for, so their
    total cannot exceed it -- past a one-minute tolerance for `duration_minutes`
    rounding to the nearest whole minute, the only account left is a fixture whose
    numbers were carried in from a different session, the same failure mode found
    above. No committed overlay has tripped either check yet; both are proven below
    against a deliberately broken shape, not only against what is on disk today.
    """

    def test_no_arm_brackets_an_average_from_one_side(self):
        for overlay_file in sorted(Path("evals/ab/arms").glob("*/*.json")):
            record = json.loads(overlay_file.read_text(encoding="utf-8"))
            drift = (record.get("overlay") or {}).get("run_drift")
            if not drift:
                continue
            context = harness._scenario_response(record["scenario"])["context"]
            averages = {
                actual["activity_id"]: actual
                for actual in context.get("recent_actuals") or []
            }
            for entry in drift.get("activities", []):
                actual = averages.get(entry["activity_id"])
                if actual is None:
                    continue
                for field in ("average_pace_sec_per_km", "average_hr"):
                    mean, first, last = (
                        actual.get(field),
                        entry["first_third"].get(field),
                        entry["last_third"].get(field),
                    )
                    if mean is None or first is None or last is None:
                        continue
                    with self.subTest(arm=record["arm"], activity=entry["activity_id"], field=field):
                        self.assertTrue(
                            min(first, last) <= mean <= max(first, last),
                            f"{record['arm']}: {entry['activity_id']} {field} averages "
                            f"{mean} but its thirds are {first} and {last} -- both ends "
                            f"on the same side of the average is not a run that happened",
                        )

    # `duration_minutes` is `round(moving_time_seconds / 60)` (source_intervals.py), so
    # it can sit up to thirty seconds away from the session's true length on rounding
    # alone before either reading is wrong. A floor of one full minute stays clear of
    # that noise rather than chase it -- the same margin the issue that asked for this
    # check named directly.
    _DURATION_ROUNDING_TOLERANCE_SEC = 60

    @staticmethod
    def _set_structure_activities(records):
        """Yield every (record, activity) pair any of ``records`` overlays a lift for."""
        for record in records:
            structure = (record.get("overlay") or {}).get("set_structure")
            if not structure:
                continue
            for entry in structure.get("activities", []):
                yield record, entry

    @classmethod
    def _committed_overlays(cls):
        for overlay_file in sorted(Path("evals/ab/arms").glob("*/*.json")):
            yield json.loads(overlay_file.read_text(encoding="utf-8"))

    def _assert_under_load_within_recorded(self, arm, entry):
        under_load, recorded = entry.get("under_load_sec"), entry.get("recorded_sec")
        if under_load is None or recorded is None:
            return
        self.assertLessEqual(
            under_load, recorded,
            f"{arm}: {entry['activity_id']} spends {under_load}s under load inside a "
            f"session recorded as only {recorded}s -- a rest cannot subtract time, so "
            f"under_load_sec can never exceed recorded_sec",
        )

    def _assert_recorded_does_not_outlast_the_actual(self, arm, entry, duration_minutes):
        recorded = entry.get("recorded_sec")
        if recorded is None or duration_minutes is None:
            return
        over_by = recorded - (duration_minutes * 60 + self._DURATION_ROUNDING_TOLERANCE_SEC)
        self.assertLessEqual(
            over_by, 0,
            f"{arm}: {entry['activity_id']} records {recorded}s of sets and rests "
            f"inside a session recent_actuals reports as {duration_minutes} minutes -- "
            f"that is {over_by}s more set time than the session lasted, past what "
            f"duration_minutes' own rounding can explain",
        )

    def test_no_arm_lifts_longer_than_it_was_recorded_for(self):
        for record, entry in self._set_structure_activities(self._committed_overlays()):
            with self.subTest(arm=record["arm"], activity=entry["activity_id"]):
                self._assert_under_load_within_recorded(record["arm"], entry)

    def test_no_arm_records_more_set_time_than_its_own_actual_lasted(self):
        for record, entry in self._set_structure_activities(self._committed_overlays()):
            context = harness._scenario_response(record["scenario"])["context"]
            actuals = {
                actual["activity_id"]: actual
                for actual in context.get("recent_actuals") or []
            }
            actual = actuals.get(entry["activity_id"])
            if actual is None:
                continue
            with self.subTest(arm=record["arm"], activity=entry["activity_id"]):
                self._assert_recorded_does_not_outlast_the_actual(
                    record["arm"], entry, actual.get("duration_minutes")
                )

    def test_a_lift_spending_more_time_under_load_than_recorded_is_refused(self):
        """The check must fail on the broken shape, not only pass on the good one.

        A hand-built overlay, in the exact shape a committed arm file carries: fed
        through the same extraction the tests above use, not a re-implementation of
        the assertion on bare numbers.
        """
        broken = [{
            "arm": "synthetic-broken-arm",
            "overlay": {
                "set_structure": {
                    "activities": [{
                        "activity_id": "synthetic-lift",
                        "under_load_sec": 1200,
                        "recorded_sec": 900,
                    }]
                }
            },
        }]
        with self.assertRaises(AssertionError):
            for record, entry in self._set_structure_activities(broken):
                self._assert_under_load_within_recorded(record["arm"], entry)

    def _assert_declares_the_scenarios_own_minutes(self, arm, scenario, row, minutes):
        self.assertEqual(
            minutes, row.get("planned_minutes"),
            f"{arm}: {row.get('session_id')} is overlaid as {row.get('planned_minutes')} "
            f"planned minutes while {scenario} prescribes {minutes}. An arm reshapes how "
            f"a read is presented, not what the athlete was told to do -- a figure that "
            f"drifts from the scenario is measuring a different week",
        )

    @staticmethod
    def _scenario_minutes(scenario):
        """Every session the scenario itself declares a length for, by id."""
        response = harness._scenario_response(scenario)
        minutes = {
            row["session_id"]: row["planned_minutes"]
            for row in (response.get("context") or {}).get("cycle_sessions") or []
            if isinstance(row, dict) and row.get("planned_minutes") is not None
        }
        plan = (response.get("plan_state") or {}).get("current_plan") or {}
        for session in (plan.get("week") or {}).get("sessions") or []:
            if isinstance(session, dict) and session.get("planned_minutes") is not None:
                minutes.setdefault(session["session_id"], session["planned_minutes"])
        return minutes

    @staticmethod
    def _overlaid_sessions(overlay, path=""):
        """Yield every object in an overlay that names a session and its length."""
        if isinstance(overlay, dict):
            if "session_id" in overlay and "planned_minutes" in overlay:
                yield path, overlay
            for key, value in overlay.items():
                yield from OverlayArithmeticTests._overlaid_sessions(value, f"{path}/{key}")
        elif isinstance(overlay, list):
            for index, value in enumerate(overlay):
                yield from OverlayArithmeticTests._overlaid_sessions(value, f"{path}[{index}]")

    def test_no_arm_restates_a_session_length_its_scenario_disagrees_with(self):
        """The third consistency axis, and the one ``--refresh-digest`` can launder.

        An arm's overlay is a hand-built replacement for part of a read, and several of
        these carry whole ``cycle_sessions`` rows -- ``planned_minutes`` and the
        prescription describing the same session, side by side. When the scenario's own
        figure moves and the overlay's does not, re-running ``capture-arm
        --refresh-digest`` records the new hash over the old contradiction and reports it
        as refreshed. That is exactly what issue #322 found: sixteen overlays still
        declared 35 minutes for a session the scenario prescribes as 56, next to the
        prescription string that says 8 km at 6:30-7:00/km.

        The scenario is the authority because an arm exists to change how a read is
        *presented*. An arm that genuinely needs a different week is a different scenario.
        """
        for record in self._committed_overlays():
            minutes = self._scenario_minutes(record["scenario"])
            for path, row in self._overlaid_sessions(record.get("overlay") or {}):
                declared = minutes.get(row.get("session_id"))
                if declared is None:
                    continue
                with self.subTest(arm=record["arm"], session=row.get("session_id"), at=path):
                    self._assert_declares_the_scenarios_own_minutes(
                        record["arm"], record["scenario"], row, declared
                    )

    def test_a_session_length_the_scenario_never_prescribed_is_refused(self):
        """The same proof the two checks above carry: it fails on the broken shape.

        The exact pairing found on disk -- an overlay saying 35 against a scenario saying
        56 -- fed through the same assertion rather than a restatement of it.
        """
        with self.assertRaises(AssertionError):
            self._assert_declares_the_scenarios_own_minutes(
                "synthetic-broken-arm",
                "synthetic-scenario",
                {"session_id": "run-easy-01", "planned_minutes": 35},
                56,
            )

    def test_a_recorded_duration_the_actual_never_had_is_refused(self):
        """Same proof for the second check: recorded_sec cannot outrun the actual.

        900 seconds of sets inside a session recent_actuals says lasted 10 minutes
        (600s) is 300s over, far past the one-minute rounding tolerance -- the same
        shape of bug #317 found in run_drift, carried over to set_structure.
        """
        broken = [{
            "arm": "synthetic-broken-arm",
            "overlay": {
                "set_structure": {
                    "activities": [{
                        "activity_id": "synthetic-lift",
                        "under_load_sec": 300,
                        "recorded_sec": 900,
                    }]
                }
            },
        }]
        with self.assertRaises(AssertionError):
            for record, entry in self._set_structure_activities(broken):
                self._assert_recorded_does_not_outlast_the_actual(
                    record["arm"], entry, duration_minutes=10
                )


class SuiteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.suite = harness.load_suite()
        self.scenarios = {item.name: item for item in scenarios_module.scenarios()}

    def test_every_turn_names_a_read_that_still_exists(self):
        for turn in self.suite["turns"]:
            with self.subTest(turn=turn["turn_id"]):
                self.assertIn(turn["scenario"], self.scenarios)

    def test_every_turn_names_sessions_its_own_scenario_prescribes(self):
        # A target session is what the answer is scored against; naming one no week of
        # the cycle ever held makes the prescribed-figure counts silently zero.
        for turn in self.suite["turns"]:
            texts = harness.prescribed_texts(turn["scenario"])
            for session_id in turn["target_session_ids"]:
                with self.subTest(turn=turn["turn_id"], session=session_id):
                    self.assertIn(session_id, texts)
                    self.assertTrue(texts[session_id].strip())

    def test_the_suite_covers_every_read_it_was_written_for(self):
        covered = {tag for turn in self.suite["turns"] for tag in turn["covers"]}
        self.assertEqual(set(), REQUIRED_COVERAGE - covered)

    def test_no_turn_covers_something_the_suite_never_declared(self):
        covered = {tag for turn in self.suite["turns"] for tag in turn["covers"]}
        self.assertEqual(set(), covered - REQUIRED_COVERAGE)

    def test_a_malformed_suite_is_refused_rather_than_half_read(self):
        broken = copy.deepcopy(self.suite)
        broken["arms"] = [arm for arm in broken["arms"] if arm["source"] != "live"]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "suite.json"
            path.write_text(json.dumps(broken), encoding="utf-8")
            with self.assertRaises(harness.EvalError):
                harness.load_suite(path)

    def test_two_turns_cannot_share_an_id(self):
        broken = copy.deepcopy(self.suite)
        broken["turns"].append(copy.deepcopy(broken["turns"][0]))
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "suite.json"
            path.write_text(json.dumps(broken), encoding="utf-8")
            with self.assertRaises(harness.EvalError):
                harness.load_suite(path)


class ArmTests(unittest.TestCase):
    """The frozen arms, against what this checkout builds today."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.suite = harness.load_suite()
        cls.live_id = harness.live_arm_id(cls.suite)
        cls.scenario_names = sorted({turn["scenario"] for turn in cls.suite["turns"]})
        cls.live = {name: harness._scenario_response(name) for name in cls.scenario_names}

    def test_every_frozen_arm_has_a_recording_for_every_read_a_turn_uses(self):
        for arm in self.suite["arms"]:
            if arm["source"] != "frozen":
                continue
            for name in self.scenario_names:
                with self.subTest(arm=arm["arm_id"], scenario=name):
                    self.assertTrue(harness.overlay_path(arm["arm_id"], name).is_file())

    def test_a_frozen_arm_is_still_this_checkout_with_two_fields_swapped(self):
        # The load-bearing one. `arm_response` refuses when the digest moved, so this
        # both checks the arms and checks that the refusal is reachable.
        for arm in self.suite["arms"]:
            if arm["source"] != "frozen":
                continue
            for name in self.scenario_names:
                with self.subTest(arm=arm["arm_id"], scenario=name):
                    harness.arm_response(
                        arm["arm_id"], name, self.suite, live=self.live[name]
                    )

    def test_a_build_that_moves_a_third_field_fails_instead_of_comparing(self):
        name = self.scenario_names[0]
        moved = copy.deepcopy(self.live[name])
        moved["context"]["timezone"] = "Etc/UTC"
        frozen = next(arm for arm in self.suite["arms"] if arm["source"] == "frozen")
        with self.assertRaises(harness.EvalError):
            harness.arm_response(frozen["arm_id"], name, self.suite, live=moved)

    def test_the_two_frozen_arms_differ_only_in_what_a_past_row_says_it_prescribed(self):
        # The change under test, stated as an assertion rather than as a commit message:
        # for a cycle row from before the previous week, one arm carries the text and the
        # other does not, and the rest of the row is identical.
        differing = 0
        for name in self.scenario_names:
            before = harness.arm_response(
                "prose-on-every-row", name, self.suite, live=self.live[name]
            )["context"]["cycle_sessions"]
            after = harness.arm_response(
                "prose-window-two-weeks", name, self.suite, live=self.live[name]
            )["context"]["cycle_sessions"]
            self.assertEqual(len(before), len(after), name)
            for old, new in zip(before, after):
                self.assertEqual(old["session_id"], new["session_id"])
                self.assertIn("prescription", old)
                self.assertTrue(str(old["prescription"]).strip())
                if "prescription" not in new:
                    differing += 1
                stripped_old = {k: v for k, v in old.items() if k != "prescription"}
                stripped_new = {k: v for k, v in new.items() if k != "prescription"}
                # `activity_id` moves with the prescription: same rule, same rows.
                for row in (stripped_old, stripped_new):
                    if isinstance(row.get("activity"), dict):
                        row["activity"] = {
                            k: v for k, v in row["activity"].items() if k != "activity_id"
                        }
                self.assertEqual(stripped_old, stripped_new, name)
        self.assertGreater(differing, 0, "no arm difference left to measure")

    def test_the_live_arm_is_whatever_this_checkout_builds(self):
        name = self.scenario_names[0]
        self.assertEqual(
            self.live[name],
            harness.arm_response(self.live_id, name, self.suite, live=self.live[name]),
        )


class AllSuitesArmTests(unittest.TestCase):
    """Every suite file, not just the bundled default -- the same digest check as above.

    ``ArmTests`` only ever calls ``harness.load_suite()`` with no argument, which checks
    ``suite.json``'s own frozen arms and nothing else. ``execution-truth-ab``,
    ``strength-labels-ab`` and ``late-cycle-segment-window-ab`` live beside it under
    ``suites/``, each names its own frozen arms, and none of them is exercised by
    anything that runs before ``create-run`` -- so a frozen arm there could go stale
    silently until someone tries to build a run from it. This repeats the one
    load-bearing assertion from ``ArmTests`` -- a frozen arm's overlay still
    reconstructs this checkout's own response with only its declared fields swapped,
    the same digest path ``arm_response`` enforces on every real run -- against every
    suite file in the repository, not only the one this module happens to default to.
    """

    SUITES_DIR = harness.SUITE_PATH.parent / "suites"

    @classmethod
    def setUpClass(cls) -> None:
        cls.suite_paths = [harness.SUITE_PATH] + sorted(cls.SUITES_DIR.glob("*.json"))

    def test_suites_directory_is_not_silently_empty(self):
        # Not a fixture of this test: an empty suites/ would make the test below pass
        # by testing nothing, which is the same silent rot this file exists to catch.
        self.assertGreater(len(self.suite_paths), 1)

    def test_every_frozen_arm_in_every_suite_loads_against_the_current_build(self):
        for suite_path in self.suite_paths:
            suite = harness.load_suite(suite_path)
            scenario_names = sorted({turn["scenario"] for turn in suite["turns"]})
            live = {name: harness._scenario_response(name) for name in scenario_names}
            for arm in suite["arms"]:
                if arm["source"] != "frozen":
                    continue
                for name in scenario_names:
                    with self.subTest(
                        suite=suite_path.name, arm=arm["arm_id"], scenario=name
                    ):
                        harness.arm_response(arm["arm_id"], name, suite, live=live[name])


class RunStoreTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.suite = harness.load_suite()

    def _run(self, tmp: str, *, turns: list[str] | None = None) -> Path:
        return harness.create_run(
            run_id="test-run",
            run_root=Path(tmp) / "runs",
            turn_ids=turns or ["today"],
        )

    def test_a_run_inside_the_repository_is_refused(self):
        with self.assertRaises(harness.EvalError):
            harness.create_run(run_id="nope", run_root=harness.ROOT / "evals" / "runs")

    def test_a_packet_never_says_which_arm_it_is(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = self._run(tmp)
            manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
            arm_ids = [arm["arm_id"] for arm in manifest["arms"]]
            for entry in manifest["packets"]:
                text = (run_dir / entry["path"]).read_text(encoding="utf-8")
                for arm_id in arm_ids:
                    with self.subTest(packet=entry["packet_id"], arm=arm_id):
                        self.assertNotIn(arm_id, text)

    def test_a_packet_carries_the_whole_read_and_both_served_texts(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = self._run(tmp)
            manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
            packet = json.loads(
                (run_dir / manifest["packets"][0]["path"]).read_text(encoding="utf-8")
            )
            self.assertIn("cycle_sessions", packet["start_coach_session"]["context"])
            self.assertIn("startCoachSession", packet["materials"]["orchestration"])
            self.assertTrue(packet["materials"]["training_judgment"].strip())
            self.assertTrue(packet["athlete_says"].strip())

    def test_the_same_run_id_builds_the_same_packet_bytes(self):
        with tempfile.TemporaryDirectory() as one, tempfile.TemporaryDirectory() as two:
            first = self._run(one)
            second = self._run(two)
            firsts = {path.name: path.read_text(encoding="utf-8") for path in sorted((first / "packets").glob("*.json"))}
            seconds = {path.name: path.read_text(encoding="utf-8") for path in sorted((second / "packets").glob("*.json"))}
            self.assertEqual(firsts, seconds)

    def test_an_existing_run_is_never_written_over(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._run(tmp)
            with self.assertRaises(harness.EvalError):
                self._run(tmp)

    def test_an_answer_without_a_named_model_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = self._run(tmp)
            packet_id = self._first_packet(run_dir)
            with self.assertRaises(harness.EvalError):
                harness.record_response(
                    run_dir,
                    packet_id,
                    _with_packet_echo("答案", packet_id),
                    {"provider": "anthropic", "model": ""},
                )

    def test_an_empty_answer_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = self._run(tmp)
            packet_id = self._first_packet(run_dir)
            with self.assertRaises(harness.EvalError):
                harness.record_response(
                    run_dir,
                    packet_id,
                    _with_packet_echo("   \n", packet_id),
                    {"provider": "anthropic", "model": "claude-opus-5"},
                )

    def test_a_recorded_answer_is_never_written_over(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = self._run(tmp)
            packet_id = self._first_packet(run_dir)
            executor = {"provider": "anthropic", "model": "claude-opus-5"}
            harness.record_response(
                run_dir, packet_id, _with_packet_echo("第一次的答案", packet_id), executor
            )
            with self.assertRaises(harness.EvalError):
                harness.record_response(
                    run_dir, packet_id, _with_packet_echo("改過的答案", packet_id), executor
                )

    def test_an_edited_packet_stops_accepting_answers(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = self._run(tmp)
            packet_id = self._first_packet(run_dir)
            path = run_dir / "packets" / f"{packet_id}.json"
            packet = json.loads(path.read_text(encoding="utf-8"))
            packet["athlete_says"] = "換一個問題"
            path.write_text(json.dumps(packet, ensure_ascii=False), encoding="utf-8")
            with self.assertRaises(harness.EvalError):
                harness.record_response(
                    run_dir, packet_id, "答案", {"provider": "anthropic", "model": "claude-opus-5"}
                )

    def test_two_models_across_the_arms_is_reported_as_not_comparable(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = self._run(tmp)
            manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
            for index, entry in enumerate(manifest["packets"]):
                harness.record_response(
                    run_dir,
                    entry["packet_id"],
                    _with_packet_echo("今天照課表跑。", entry["packet_id"]),
                    {"provider": "anthropic", "model": f"model-{index}"},
                )
            value = harness.report(run_dir)
            self.assertFalse(value["comparable"])
            self.assertIn("more than one model", value["not_comparable_because"])

    def test_one_model_across_the_arms_compares(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = self._run(tmp)
            manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
            for entry in manifest["packets"]:
                harness.record_response(
                    run_dir,
                    entry["packet_id"],
                    _with_packet_echo("今天照課表跑。", entry["packet_id"]),
                    {"provider": "anthropic", "model": "claude-opus-5"},
                )
            value = harness.report(run_dir)
            self.assertTrue(value["comparable"])
            self.assertEqual(value["answered"], value["of"])
            self.assertIn("today", harness.render_report(value))

    def test_the_run_records_whether_an_arm_is_the_live_build(self):
        # Before the builder changes, one frozen arm is the live one. That is a fact
        # about the run and belongs in it -- a report read later should say whether the
        # instrument was reading zero at the time.
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = self._run(tmp)
            manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
            self.assertIn("arms_identical_to_live", manifest)
            self.assertNotIn(
                harness.live_arm_id(self.suite), manifest["arms_identical_to_live"]
            )

    @staticmethod
    def _first_packet(run_dir: Path) -> str:
        manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
        return manifest["packets"][0]["packet_id"]


class PacketEchoTests(unittest.TestCase):
    """The answer must prove which packet it was actually produced from -- issue #322.

    The incident: a blind-answer run whose packet path did not resolve, so the answerer
    found and read a leftover packet from somewhere else and answered that instead.
    record-response only ever knew the packet id typed on its command line, never what
    the answer in front of it was actually produced from -- three recorded "samples"
    turned out to be readings of the previous run's packet, and nothing caught it.

    The fix asks one more thing of the answer, through the packet's own instructions:
    close with this packet's own id, in the answerer's own words. record-response checks
    that line against the packet it is filing the answer under, so an answer produced
    from a different packet -- which can only ever echo *that* packet's id -- is refused
    here instead of filed silently under the wrong content.
    """

    def _run(self, tmp: str) -> Path:
        return harness.create_run(
            run_id="echo-run", run_root=Path(tmp) / "runs", turn_ids=["today"]
        )

    @staticmethod
    def _first_packet(run_dir: Path) -> str:
        manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
        return manifest["packets"][0]["packet_id"]

    def test_a_correct_echo_is_accepted_and_stripped_from_what_is_stored_and_counted(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = self._run(tmp)
            packet_id = self._first_packet(run_dir)
            body = "今天照課表跑，5 公里，配速 5:30。"
            path = harness.record_response(
                run_dir,
                packet_id,
                f"{body}\npacket: {packet_id}",
                {"provider": "anthropic", "model": "claude-opus-5"},
            )
            recorded = json.loads(path.read_text(encoding="utf-8"))
            # The stored answer is exactly the coach's words -- the echo line is gone
            # from it, not merely tolerated inside it.
            self.assertEqual(body, recorded["answer"])
            self.assertNotIn("packet:", recorded["answer"])

            value = harness.report(run_dir)
            row = next(r for r in value["rows"] if r["packet_id"] == packet_id)
            sample = row["samples"][0]
            self.assertEqual(body, sample["answer"])
            # The bookkeeping line never inflates the count it rides along with.
            self.assertEqual(len(body), sample["signals"]["answer_characters"])

    def test_an_echo_naming_a_different_packet_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = self._run(tmp)
            manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
            packet_ids = [entry["packet_id"] for entry in manifest["packets"]]
            self.assertGreater(len(packet_ids), 1, "need a second packet to misname")
            target, other = packet_ids[0], packet_ids[1]
            with self.assertRaises(harness.EvalError) as caught:
                harness.record_response(
                    run_dir,
                    target,
                    f"今天照課表跑。\npacket: {other}",
                    {"provider": "anthropic", "model": "claude-opus-5"},
                )
            # The message names the packet the answer was supposed to be filed under,
            # not just "something is wrong" -- what a reviewer needs to fix it.
            self.assertIn(target, str(caught.exception))

    def test_a_missing_echo_on_a_packet_that_asks_for_one_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = self._run(tmp)
            packet_id = self._first_packet(run_dir)
            with self.assertRaises(harness.EvalError):
                harness.record_response(
                    run_dir,
                    packet_id,
                    "今天照課表跑，沒有附上回聲行。",
                    {"provider": "anthropic", "model": "claude-opus-5"},
                )

    def test_a_packet_predating_the_echo_requirement_accepts_a_plain_answer(self):
        # Simulates a run created before PACKET_ECHO_INSTRUCTION existed: this packet's
        # own instructions never asked for the line (the file on disk is what decides,
        # not whatever this checkout currently defines), so record-response must not
        # start refusing an answer that was never asked to carry one.
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = self._run(tmp)
            packet_id = self._first_packet(run_dir)
            packet_path = run_dir / "packets" / f"{packet_id}.json"
            packet = json.loads(packet_path.read_text(encoding="utf-8"))
            self.assertIn(harness.PACKET_ECHO_INSTRUCTION, packet["instructions"])
            packet["instructions"] = [
                line
                for line in packet["instructions"]
                if line != harness.PACKET_ECHO_INSTRUCTION
            ]
            packet_path.write_text(json.dumps(packet, ensure_ascii=False), encoding="utf-8")
            # The packet's bytes moved on purpose (it now reads as an older packet), so
            # its recorded digest has to move with it -- this is the fixture standing in
            # for a run that predates the instruction, not tampering to detect.
            manifest_path = run_dir / "manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            for entry in manifest["packets"]:
                if entry["packet_id"] == packet_id:
                    entry["sha256"] = harness._sha(packet)
            manifest_path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")

            plain = "今天照課表跑，沒有回聲行也照收。"
            path = harness.record_response(
                run_dir, packet_id, plain, {"provider": "anthropic", "model": "claude-opus-5"}
            )
            recorded = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(plain, recorded["answer"])


class FigureTests(unittest.TestCase):
    def test_a_figure_needs_a_unit(self):
        self.assertEqual({"70"}, figures.figures_in_text("深蹲 70 公斤"))
        self.assertEqual(set(), figures.figures_in_text("第 3 週結束"))

    def test_a_pace_is_read_as_a_pace_and_a_date_is_not_a_figure(self):
        self.assertEqual({"5:33"}, figures.figures_in_text("平均配速 5:33/km"))
        self.assertEqual(set(), figures.figures_in_text("2026-08-13 那天"))

    def test_a_scheme_states_both_of_its_numbers(self):
        self.assertEqual({"5"}, figures.figures_in_text("臥推 5x5"))
        self.assertEqual({"4", "6"}, figures.figures_in_text("深蹲 4x6"))

    def test_minutes_do_not_read_as_metres(self):
        self.assertEqual({"12"}, figures.figures_in_text("Warm-up 12 min"))

    def test_a_pace_stored_in_seconds_supports_the_way_it_is_said(self):
        supported = figures.supported_figures({"average_pace_sec_per_km": 333})
        self.assertIn("5:33", supported)
        self.assertEqual([], figures.unsupported_figures("配速 5:33/km", supported))

    def test_a_distance_stored_in_metres_supports_the_way_it_is_said(self):
        supported = figures.supported_figures({"duration": {"meters": 8000}})
        self.assertEqual([], figures.unsupported_figures("8 公里", supported))

    def test_a_load_nothing_carries_is_reported(self):
        supported = figures.supported_figures({"load_kg": 70.0})
        self.assertEqual(["82.5"], figures.unsupported_figures("下次上 82.5 公斤", supported))

    def test_a_figure_inside_a_prescription_string_counts_as_carried(self):
        supported = figures.supported_figures(
            {"prescription": "Warm-up 12分\n5趟：Interval 1公里 配速 6:00/km"}
        )
        self.assertEqual(
            [], figures.unsupported_figures("熱身 12 分，5 趟 1 公里配速 6:00/km", supported)
        )

    def test_seventy_point_zero_and_seventy_are_one_figure(self):
        supported = figures.supported_figures({"load_kg": 70.0})
        self.assertEqual([], figures.unsupported_figures("70 公斤", supported))

    def test_the_unsupported_list_reads_in_a_stable_order(self):
        supported: set[str] = set()
        self.assertEqual(
            ["8", "70", "5:33"],
            figures.unsupported_figures("70 公斤、8 公里、配速 5:33/km", supported),
        )

    def test_a_compact_segment_row_supports_its_clock_and_km_readings(self):
        # segment_rows is a bare positional list -- ["WORK", 1000.0, 358] -- so the
        # generic walk sees every cell under the parent key `segment_rows` and none of
        # the *_sec/meters suffix matching can fire on it. segment_fields says what
        # each position means, so 358 (moving_time_sec) reads aloud as "5:58" the same
        # way a dict-shaped segment's moving_time_sec would, and 1000.0 (distance_m)
        # reads as "1" 公里 the same way a dict-shaped segment's meters field would.
        activity = {
            "activity_id": "intervals:1",
            "date": "2026-08-13",
            "sport": "running",
            "segment_fields": ["provider_type", "distance_m", "moving_time_sec"],
            "segment_rows": [["WORK", 1000.0, 358]],
        }
        supported = figures.supported_figures(
            {"segment_execution": {"activities": [activity]}}
        )
        self.assertIn("5:58", supported)
        self.assertEqual([], figures.unsupported_figures("5:58/km", supported))
        self.assertIn("1", supported)
        self.assertEqual([], figures.unsupported_figures("1 公里", supported))

    def test_a_compact_segment_row_still_supports_its_bare_numbers(self):
        # The fix only adds derivations; the raw-number reading the generic walk
        # already produced for a segment_rows cell must still be there afterwards.
        activity = {
            "activity_id": "intervals:1",
            "date": "2026-08-13",
            "sport": "running",
            "segment_fields": ["provider_type", "distance_m", "moving_time_sec"],
            "segment_rows": [["WORK", 1000.0, 358]],
        }
        supported = figures.supported_figures(
            {"segment_execution": {"activities": [activity]}}
        )
        self.assertIn("1000", supported)
        self.assertIn("358", supported)

    def test_a_compact_segment_row_does_not_invent_a_cross_row_sum(self):
        # Two rows' elapsed times summed (240 + 218 = 458s = "7:38") is exactly the
        # cross-row arithmetic the README says a derivation must never manufacture --
        # only the per-row values it already carries.
        activity = {
            "activity_id": "intervals:1",
            "date": "2026-08-13",
            "sport": "running",
            "segment_fields": ["provider_type", "distance_m", "moving_time_sec"],
            "segment_rows": [["WORK", 527.0, 240], ["WORK", 464.0, 218]],
        }
        supported = figures.supported_figures(
            {"segment_execution": {"activities": [activity]}}
        )
        self.assertNotIn("7:38", supported)
        self.assertEqual(["7:38"], figures.unsupported_figures("緩和跑 7:38", supported))


class SignalTests(unittest.TestCase):
    """What the harness measures about one answer, on a known arm and a known turn."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.suite = harness.load_suite()
        cls.turn = next(
            turn
            for turn in cls.suite["turns"]
            if turn["turn_id"] == "week-one-long-run-what-was-it"
        )
        live = harness._scenario_response(cls.turn["scenario"])
        cls.packets = {
            arm["arm_id"]: harness.build_packet(
                run_id="signal-test",
                arm_id=arm["arm_id"],
                turn=cls.turn,
                response=harness.arm_response(
                    arm["arm_id"], cls.turn["scenario"], cls.suite, live=live
                ),
            )
            for arm in cls.suite["arms"]
        }

    def test_the_prescription_is_in_one_arm_and_not_the_other(self):
        # The turn is the one whose target session is stated on its own row and nowhere
        # else in the cycle, so dropping the row's text drops the figures outright.
        with_prose = harness.signals(
            answer="", packet=self.packets["prose-on-every-row"], turn=self.turn
        )
        without = harness.signals(
            answer="", packet=self.packets["prose-window-two-weeks"], turn=self.turn
        )
        self.assertGreater(with_prose["prescribed_figures_total"], 0)
        self.assertEqual(
            with_prose["prescribed_figures_total"],
            with_prose["prescribed_figures_in_this_arm"],
        )
        self.assertLess(
            without["prescribed_figures_in_this_arm"],
            with_prose["prescribed_figures_in_this_arm"],
        )
        self.assertEqual(
            with_prose["prescribed_figures_total"], without["prescribed_figures_total"]
        )

    def test_an_answer_that_states_the_prescription_is_counted_as_having_stated_it(self):
        answer = "第一週的長跑排的是 12 公里，配速 6:40-7:10/km。"
        measured = harness.signals(
            answer=answer, packet=self.packets["prose-on-every-row"], turn=self.turn
        )
        self.assertEqual(
            measured["prescribed_figures_total"], measured["prescribed_figures_stated"]
        )
        self.assertEqual([], measured["figures_not_in_the_context"])

    def test_a_stated_prescription_the_arm_never_carried_shows_up_as_unsupported(self):
        answer = "第一週的長跑排的是 12 公里，配速 6:40-7:10/km。"
        measured = harness.signals(
            answer=answer, packet=self.packets["prose-window-two-weeks"], turn=self.turn
        )
        self.assertIn("6:40", measured["figures_not_in_the_context"])
        self.assertIn("7:10", measured["figures_not_in_the_context"])

    def test_asking_and_abstaining_are_both_counted(self):
        measured = harness.signals(
            answer="我這邊沒有紀錄那堂課排了什麼，你還記得嗎？",
            packet=self.packets["prose-window-two-weeks"],
            turn=self.turn,
        )
        self.assertEqual(1, measured["questions_asked"])
        self.assertEqual(1, measured["uncertainty_markers"])

    def test_explicit_declines_the_original_list_missed_are_counted(self):
        # A real answer declined with these three phrases and scored 0 uncertainty
        # markers before they were added: 回答不了 and 只查得到 matched nothing, and
        # 沒辦法 outside the narrower 沒辦法判斷 construction matched nothing either.
        answer = "這題我回答不了，沒辦法給你確切數字，目前只查得到平均值。"
        measured = harness.signals(
            answer=answer,
            packet=self.packets["prose-window-two-weeks"],
            turn=self.turn,
        )
        self.assertEqual(3, measured["uncertainty_markers"])


class ExternalSuiteTests(unittest.TestCase):
    """``--suite``: a suite JSON living outside ``evals/ab/``, at the function and CLI layers."""

    def _write_suite(self, tmp: str, *, name: str = "external-suite.json") -> Path:
        suite = copy.deepcopy(harness.load_suite())
        path = Path(tmp) / name
        path.write_text(json.dumps(suite, ensure_ascii=False), encoding="utf-8")
        return path

    def test_create_run_reads_a_suite_from_an_arbitrary_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            suite_path = self._write_suite(tmp)
            run_dir = harness.create_run(
                run_id="external-suite-run",
                run_root=Path(tmp) / "runs",
                suite_path=suite_path,
                turn_ids=["today"],
            )
            manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual("cycle-session-prescription-ab", manifest["suite"]["suite_id"])
            # The run holds its own copy, hashed into the manifest -- report and
            # record-response read that copy from here on, never the original path again.
            self.assertTrue((run_dir / "suite.json").is_file())

    def test_an_external_suite_produces_the_same_packet_bytes_as_the_bundled_one(self):
        # Same run_id in both -- packet_id is derived from run_id, so a different
        # run_id would legitimately produce different bytes regardless of the suite.
        # Two separate run_roots keep the two runs from colliding on disk.
        with tempfile.TemporaryDirectory() as bundled_tmp, tempfile.TemporaryDirectory() as external_tmp:
            bundled = harness.create_run(
                run_id="same-run-id", run_root=Path(bundled_tmp) / "runs", turn_ids=["today"]
            )
            suite_path = self._write_suite(external_tmp)
            external = harness.create_run(
                run_id="same-run-id",
                run_root=Path(external_tmp) / "runs",
                suite_path=suite_path,
                turn_ids=["today"],
            )
            bundled_packets = {
                path.name: path.read_text(encoding="utf-8")
                for path in sorted((bundled / "packets").glob("*.json"))
            }
            external_packets = {
                path.name: path.read_text(encoding="utf-8")
                for path in sorted((external / "packets").glob("*.json"))
            }
            self.assertEqual(bundled_packets, external_packets)

    def test_record_response_and_report_work_on_a_run_built_from_an_external_suite(self):
        with tempfile.TemporaryDirectory() as tmp:
            suite_path = self._write_suite(tmp)
            run_dir = harness.create_run(
                run_id="external-report",
                run_root=Path(tmp) / "runs",
                suite_path=suite_path,
                turn_ids=["today"],
            )
            manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
            executor = {"provider": "anthropic", "model": "claude-opus-5"}
            for entry in manifest["packets"]:
                harness.record_response(
                    run_dir,
                    entry["packet_id"],
                    _with_packet_echo("今天照課表跑。", entry["packet_id"]),
                    executor,
                )
            value = harness.report(run_dir)
            self.assertTrue(value["comparable"])
            self.assertEqual(value["answered"], value["of"])

    def test_cli_create_run_accepts_a_suite_flag(self):
        with tempfile.TemporaryDirectory() as tmp:
            suite_path = self._write_suite(tmp)
            run_root = Path(tmp) / "runs"
            code = harness.main(
                [
                    "create-run",
                    "--run-id", "cli-external",
                    "--run-root", str(run_root),
                    "--suite", str(suite_path),
                    "--turn", "today",
                ]
            )
            self.assertEqual(0, code)
            self.assertTrue((run_root / "cli-external" / "manifest.json").is_file())

    def test_cli_create_run_without_a_suite_flag_still_uses_the_bundled_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_root = Path(tmp) / "runs"
            code = harness.main(
                [
                    "create-run",
                    "--run-id", "cli-default",
                    "--run-root", str(run_root),
                    "--turn", "today",
                ]
            )
            self.assertEqual(0, code)
            manifest = json.loads(
                (run_root / "cli-default" / "manifest.json").read_text(encoding="utf-8")
            )
            self.assertEqual("cycle-session-prescription-ab", manifest["suite"]["suite_id"])


class OverlayFieldsTests(unittest.TestCase):
    """A suite's own ``overlay_fields`` -- absent, declared, validated, and captured against."""

    def setUp(self) -> None:
        self.suite = copy.deepcopy(harness.load_suite())
        self.scenario = "01_revisit_today__no_reconcile"

    def test_a_suite_silent_on_overlay_fields_falls_back_to_the_module_constant(self):
        # The silence is made here rather than borrowed from the bundled suite. That
        # suite declared its three fields when #306 moved `segment_execution`, and a
        # test that reads the fallback off whichever suite happens to be silent today
        # stops testing the fallback the first time one of them speaks.
        self.suite.pop("overlay_fields", None)
        self.assertEqual(harness.OVERLAY_FIELDS, harness.overlay_fields(self.suite))

    def test_a_declared_overlay_fields_is_honored(self):
        self.suite["overlay_fields"] = ["strength_execution"]
        self.assertEqual(("strength_execution",), harness.overlay_fields(self.suite))

    def test_an_empty_overlay_fields_list_is_refused_at_load(self):
        self.suite["overlay_fields"] = []
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "suite.json"
            path.write_text(json.dumps(self.suite, ensure_ascii=False), encoding="utf-8")
            with self.assertRaises(harness.EvalError):
                harness.load_suite(path)

    def test_a_non_string_entry_in_overlay_fields_is_refused_at_load(self):
        self.suite["overlay_fields"] = ["cycle_sessions", 3]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "suite.json"
            path.write_text(json.dumps(self.suite, ensure_ascii=False), encoding="utf-8")
            with self.assertRaises(harness.EvalError):
                harness.load_suite(path)

    def test_capture_arm_freezes_only_the_suites_declared_fields(self):
        # capture-arm always writes under its module's ARMS_DIR; patched here to a
        # scratch directory so this test leaves no new files under the real
        # evals/ab/arms/.
        self.suite["overlay_fields"] = ["cycle_sessions"]
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(harness, "ARMS_DIR", Path(tmp) / "arms"):
                harness.capture_arm("scratch-single-field", None, "test", self.suite)
                record = json.loads(
                    harness.overlay_path("scratch-single-field", self.scenario).read_text(
                        encoding="utf-8"
                    )
                )
                self.assertEqual({"cycle_sessions"}, set(record["overlay"].keys()))

    def test_a_captured_single_field_arm_round_trips_through_arm_response(self):
        self.suite["overlay_fields"] = ["cycle_sessions"]
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(harness, "ARMS_DIR", Path(tmp) / "arms"):
                harness.capture_arm("scratch-single-field", None, "test", self.suite)
                live = harness._scenario_response(self.scenario)
                overlaid = harness.arm_response(
                    "scratch-single-field", self.scenario, self.suite, live=live
                )
                # The declared field is honestly reconstructed through the overlay...
                self.assertEqual(
                    live["context"]["cycle_sessions"], overlaid["context"]["cycle_sessions"]
                )
                # ...and a field this suite never declared travels with the rest of the
                # read instead, rather than being silently dropped.
                self.assertEqual(
                    live["context"].get("recent_actuals"),
                    overlaid["context"].get("recent_actuals"),
                )


class RefreshDigestTests(unittest.TestCase):
    """``capture-arm --refresh-digest`` -- the repair a new top-level context key needs.

    ``ArmTests`` and ``AllSuitesArmTests`` already prove every committed arm loads
    against this checkout, which means their recorded digest is already correct. What
    is untested until now is the refresh operation itself: byte-stable when nothing
    needs to change, touching only the digest field when something does, and able to
    repair a digest a build change broke -- the one case ``capture_arm`` alone cannot
    fix, because a checkout of the arm's own commit never has the new key to hash
    (README.md, "When a new top-level context key stops every arm"; verified for #28).
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.suite = harness.load_suite()

    def test_refreshing_every_committed_arm_is_a_no_op(self):
        # Against a copy of evals/ab/arms, not the real tree -- this test's job is to
        # prove the no-op property, not to repair the committed arms if they were ever
        # stale (AllSuitesArmTests fails loudly first if that happens).
        suite_paths = [harness.SUITE_PATH] + sorted(
            (harness.SUITE_PATH.parent / "suites").glob("*.json")
        )
        with tempfile.TemporaryDirectory() as tmp:
            copy_of_arms = Path(tmp) / "arms"
            shutil.copytree(harness.ARMS_DIR, copy_of_arms)
            before = {p: p.read_bytes() for p in sorted(copy_of_arms.glob("*/*.json"))}
            with mock.patch.object(harness, "ARMS_DIR", copy_of_arms):
                for suite_path in suite_paths:
                    suite = harness.load_suite(suite_path)
                    with self.subTest(suite=suite_path.name):
                        results = harness.refresh_all_arm_digests(suite)
                        self.assertTrue(results)
                        self.assertFalse(any(r["changed"] for r in results))
            after = {p: p.read_bytes() for p in before}
            self.assertEqual(before, after)

    def test_refresh_repairs_a_corrupted_digest_back_to_byte_identical(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(harness, "ARMS_DIR", Path(tmp) / "arms"):
                scenario = "01_revisit_today__no_reconcile"
                harness.capture_arm("scratch-repair", None, "note text", self.suite)
                path = harness.overlay_path("scratch-repair", scenario)
                original_text = path.read_text(encoding="utf-8")

                # Corrupt the digest the way a stale build would -- the overlay is
                # real, honest content; only what it is checked against went stale.
                corrupted = json.loads(original_text)
                corrupted["untouched_sha256"] = "0" * 64
                corrupted_text = harness._dump(corrupted)
                path.write_text(corrupted_text, encoding="utf-8")

                results = harness.refresh_arm_digest("scratch-repair", self.suite)
                result = next(r for r in results if r["scenario"] == scenario)
                self.assertTrue(result["changed"])

                repaired_text = path.read_text(encoding="utf-8")
                # Nothing about the live build moved between the two calls, so the
                # repair lands on exactly the file capture_arm wrote in the first place.
                self.assertEqual(original_text, repaired_text)

                # And against the corrupted copy, the only line that moved is the digest.
                corrupted_lines = corrupted_text.splitlines()
                repaired_lines = repaired_text.splitlines()
                self.assertEqual(len(corrupted_lines), len(repaired_lines))
                differing = [
                    i
                    for i, (a, b) in enumerate(zip(corrupted_lines, repaired_lines))
                    if a != b
                ]
                self.assertEqual(1, len(differing))
                self.assertIn("untouched_sha256", corrupted_lines[differing[0]])

    def test_refresh_is_a_no_op_once_the_digest_is_already_correct(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(harness, "ARMS_DIR", Path(tmp) / "arms"):
                harness.capture_arm("scratch-idempotent", None, "", self.suite)
                scenario = sorted({t["scenario"] for t in self.suite["turns"]})[0]
                path = harness.overlay_path("scratch-idempotent", scenario)
                before = path.read_bytes()

                results = harness.refresh_arm_digest("scratch-idempotent", self.suite)
                result = next(r for r in results if r["scenario"] == scenario)
                self.assertFalse(result["changed"])
                self.assertEqual(before, path.read_bytes())

    def test_a_digest_broken_by_a_stale_capture_loads_again_after_refresh(self):
        # The exact failure #309 exists for: a build change (standing in here for a new
        # top-level context key) makes the recorded digest stale, and arm_response
        # refuses to serve the overlay until it is repaired.
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(harness, "ARMS_DIR", Path(tmp) / "arms"):
                scenario = "01_revisit_today__no_reconcile"
                harness.capture_arm("scratch-stale", None, "", self.suite)
                path = harness.overlay_path("scratch-stale", scenario)
                record = json.loads(path.read_text(encoding="utf-8"))
                record["untouched_sha256"] = "0" * 64
                path.write_text(harness._dump(record), encoding="utf-8")

                live = harness._scenario_response(scenario)
                with self.assertRaises(harness.EvalError):
                    harness.arm_response("scratch-stale", scenario, self.suite, live=live)

                harness.refresh_arm_digest("scratch-stale", self.suite)

                # Loads clean now -- no exception is the assertion.
                harness.arm_response("scratch-stale", scenario, self.suite, live=live)

    def test_refresh_all_skips_the_live_arm(self):
        # The live arm has no overlay file at all -- refresh_all_arm_digests must
        # never try to read one for it.
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(harness, "ARMS_DIR", Path(tmp) / "arms"):
                for arm in self.suite["arms"]:
                    if arm["source"] == "frozen":
                        harness.capture_arm(arm["arm_id"], None, "", self.suite)
                results = harness.refresh_all_arm_digests(self.suite)  # no raise
                self.assertNotIn(
                    harness.live_arm_id(self.suite), {r["arm"] for r in results}
                )

    def test_cli_refresh_digest_without_arm_covers_every_frozen_arm_in_the_suite(self):
        with tempfile.TemporaryDirectory() as tmp:
            # ROOT patched alongside ARMS_DIR: in real use ARMS_DIR always sits under
            # ROOT (evals/ab/arms), which is what lets the CLI's own printout report a
            # path relative to it -- true of any real checkout, so the fixture keeps it
            # true here too rather than papering over the CLI layer with a bare ARMS_DIR
            # patch that a system tmpdir would leave dangling outside ROOT.
            with mock.patch.object(harness, "ARMS_DIR", Path(tmp) / "arms"), mock.patch.object(
                harness, "ROOT", Path(tmp)
            ):
                frozen_ids = [
                    a["arm_id"] for a in self.suite["arms"] if a["source"] == "frozen"
                ]
                self.assertEqual(2, len(frozen_ids))
                scenario = sorted({t["scenario"] for t in self.suite["turns"]})[0]
                for arm_id in frozen_ids:
                    harness.capture_arm(arm_id, None, "", self.suite)
                    path = harness.overlay_path(arm_id, scenario)
                    record = json.loads(path.read_text(encoding="utf-8"))
                    record["untouched_sha256"] = "0" * 64
                    path.write_text(harness._dump(record), encoding="utf-8")

                suite_path = Path(tmp) / "suite.json"
                suite_path.write_text(
                    json.dumps(self.suite, ensure_ascii=False), encoding="utf-8"
                )
                code = harness.main(
                    ["capture-arm", "--refresh-digest", "--suite", str(suite_path)]
                )
                self.assertEqual(0, code)

                live = harness._scenario_response(scenario)
                for arm_id in frozen_ids:
                    harness.arm_response(arm_id, scenario, self.suite, live=live)

    def test_cli_refresh_digest_with_arm_only_touches_that_arm(self):
        with tempfile.TemporaryDirectory() as tmp:
            # See the previous test for why ROOT moves with ARMS_DIR here.
            with mock.patch.object(harness, "ARMS_DIR", Path(tmp) / "arms"), mock.patch.object(
                harness, "ROOT", Path(tmp)
            ):
                frozen_ids = [
                    a["arm_id"] for a in self.suite["arms"] if a["source"] == "frozen"
                ]
                target, other = frozen_ids[0], frozen_ids[1]
                scenario = sorted({t["scenario"] for t in self.suite["turns"]})[0]
                for arm_id in frozen_ids:
                    harness.capture_arm(arm_id, None, "", self.suite)
                    path = harness.overlay_path(arm_id, scenario)
                    record = json.loads(path.read_text(encoding="utf-8"))
                    record["untouched_sha256"] = "0" * 64
                    path.write_text(harness._dump(record), encoding="utf-8")

                suite_path = Path(tmp) / "suite.json"
                suite_path.write_text(
                    json.dumps(self.suite, ensure_ascii=False), encoding="utf-8"
                )
                code = harness.main(
                    [
                        "capture-arm", "--refresh-digest",
                        "--arm", target,
                        "--suite", str(suite_path),
                    ]
                )
                self.assertEqual(0, code)

                live = harness._scenario_response(scenario)
                harness.arm_response(target, scenario, self.suite, live=live)
                with self.assertRaises(harness.EvalError):
                    harness.arm_response(other, scenario, self.suite, live=live)

    def test_cli_refresh_digest_rejects_commit_and_note(self):
        for extra in (["--commit", "abc123"], ["--note", "why"]):
            with self.subTest(extra=extra):
                with contextlib.redirect_stderr(io.StringIO()):
                    code = harness.main(
                        ["capture-arm", "--refresh-digest", "--arm", "does-not-matter"]
                        + extra
                    )
                self.assertEqual(2, code)

    def test_cli_capture_arm_requires_arm_without_refresh_digest(self):
        with contextlib.redirect_stderr(io.StringIO()):
            code = harness.main(["capture-arm"])
        self.assertEqual(2, code)


class SingleArmSuiteTests(unittest.TestCase):
    """The harness never assumed a fixed number of arms -- a suite says how many."""

    def test_create_run_accepts_a_suite_with_only_the_live_arm(self):
        suite = copy.deepcopy(harness.load_suite())
        suite["arms"] = [arm for arm in suite["arms"] if arm["source"] == "live"]
        with tempfile.TemporaryDirectory() as tmp:
            suite_path = Path(tmp) / "single-arm-suite.json"
            suite_path.write_text(json.dumps(suite, ensure_ascii=False), encoding="utf-8")
            run_dir = harness.create_run(
                run_id="single-arm",
                run_root=Path(tmp) / "runs",
                suite_path=suite_path,
                turn_ids=["today"],
            )
            manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(1, len(manifest["arms"]))
            self.assertEqual(1, len(manifest["packets"]))
            self.assertEqual([], manifest["arms_identical_to_live"])

    def test_a_two_arm_suite_also_works(self):
        # Not pinned to one and not pinned to three -- whatever the suite declares.
        suite = copy.deepcopy(harness.load_suite())
        keep = {arm["arm_id"] for arm in suite["arms"] if arm["source"] == "live"}
        keep.add(next(arm["arm_id"] for arm in suite["arms"] if arm["source"] == "frozen"))
        suite["arms"] = [arm for arm in suite["arms"] if arm["arm_id"] in keep]
        with tempfile.TemporaryDirectory() as tmp:
            suite_path = Path(tmp) / "two-arm-suite.json"
            suite_path.write_text(json.dumps(suite, ensure_ascii=False), encoding="utf-8")
            run_dir = harness.create_run(
                run_id="two-arm",
                run_root=Path(tmp) / "runs",
                suite_path=suite_path,
                turn_ids=["today"],
            )
            manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(2, len(manifest["arms"]))
            self.assertEqual(2, len(manifest["packets"]))


class SampleTests(unittest.TestCase):
    """Repeated answers to one packet -- issue #86's entry point for a consistency read."""

    def _run(self, tmp: str) -> Path:
        return harness.create_run(
            run_id="sample-run", run_root=Path(tmp) / "runs", turn_ids=["today"]
        )

    @staticmethod
    def _first_packet(run_dir: Path) -> str:
        manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
        return manifest["packets"][0]["packet_id"]

    def test_the_default_call_still_writes_the_pre_sampling_file_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = self._run(tmp)
            packet_id = self._first_packet(run_dir)
            path = harness.record_response(
                run_dir,
                packet_id,
                _with_packet_echo("答案", packet_id),
                {"provider": "anthropic", "model": "claude-opus-5"},
            )
            self.assertEqual(f"{packet_id}.json", path.name)

    def test_a_second_and_third_sample_are_named_explicitly_and_do_not_collide(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = self._run(tmp)
            packet_id = self._first_packet(run_dir)
            executor = {"provider": "anthropic", "model": "claude-opus-5"}
            first = harness.record_response(
                run_dir, packet_id, _with_packet_echo("第一次答案", packet_id), executor, sample=1
            )
            second = harness.record_response(
                run_dir, packet_id, _with_packet_echo("第二次答案", packet_id), executor, sample=2
            )
            third = harness.record_response(
                run_dir, packet_id, _with_packet_echo("第三次答案", packet_id), executor, sample=3
            )
            self.assertEqual(3, len({first, second, third}))
            for path in (first, second, third):
                self.assertTrue(path.is_file())

    def test_naming_a_sample_already_recorded_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = self._run(tmp)
            packet_id = self._first_packet(run_dir)
            executor = {"provider": "anthropic", "model": "claude-opus-5"}
            harness.record_response(
                run_dir, packet_id, _with_packet_echo("第一次", packet_id), executor, sample=2
            )
            with self.assertRaises(harness.EvalError):
                harness.record_response(
                    run_dir,
                    packet_id,
                    _with_packet_echo("重寫第二次", packet_id),
                    executor,
                    sample=2,
                )

    def test_sample_zero_or_negative_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = self._run(tmp)
            packet_id = self._first_packet(run_dir)
            executor = {"provider": "anthropic", "model": "claude-opus-5"}
            with self.assertRaises(harness.EvalError):
                harness.record_response(
                    run_dir, packet_id, _with_packet_echo("答案", packet_id), executor, sample=0
                )

    def test_report_lists_every_sample_of_one_packet_side_by_side(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = self._run(tmp)
            manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
            executor = {"provider": "anthropic", "model": "claude-opus-5"}
            target = manifest["packets"][0]["packet_id"]
            harness.record_response(
                run_dir, target, _with_packet_echo("第一次照課表跑。", target), executor, sample=1
            )
            harness.record_response(
                run_dir,
                target,
                _with_packet_echo("第二次照課表跑，用詞不同。", target),
                executor,
                sample=2,
            )
            for entry in manifest["packets"][1:]:
                harness.record_response(
                    run_dir,
                    entry["packet_id"],
                    _with_packet_echo("照課表跑。", entry["packet_id"]),
                    executor,
                )

            value = harness.report(run_dir)
            row = next(r for r in value["rows"] if r["packet_id"] == target)
            self.assertEqual(2, len(row["samples"]))
            self.assertEqual({1, 2}, {s["sample"] for s in row["samples"]})
            answers = {s["sample"]: s["answer"] for s in row["samples"]}
            self.assertEqual("第一次照課表跑。", answers[1])
            self.assertEqual("第二次照課表跑，用詞不同。", answers[2])
            self.assertTrue(all("signals" in s for s in row["samples"]))
            # Still answered once per packet, not once per sample.
            self.assertEqual(value["answered"], value["of"])

    def test_the_report_never_averages_or_scores_a_verdict_across_samples(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = self._run(tmp)
            manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
            executor = {"provider": "anthropic", "model": "claude-opus-5"}
            target = manifest["packets"][0]["packet_id"]
            harness.record_response(
                run_dir, target, _with_packet_echo("第一次答案。", target), executor, sample=1
            )
            harness.record_response(
                run_dir, target, _with_packet_echo("第二次答案。", target), executor, sample=2
            )
            for entry in manifest["packets"][1:]:
                harness.record_response(
                    run_dir,
                    entry["packet_id"],
                    _with_packet_echo("答案。", entry["packet_id"]),
                    executor,
                )
            blob = json.dumps(harness.report(run_dir)).lower()
            for word in ("average", "mean", "verdict", "score"):
                self.assertNotIn(word, blob)

    def test_render_report_labels_each_sample_when_there_is_more_than_one(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = self._run(tmp)
            manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
            executor = {"provider": "anthropic", "model": "claude-opus-5"}
            target = manifest["packets"][0]["packet_id"]
            harness.record_response(
                run_dir, target, _with_packet_echo("第一次照課表跑。", target), executor, sample=1
            )
            harness.record_response(
                run_dir, target, _with_packet_echo("第二次照課表跑。", target), executor, sample=2
            )
            for entry in manifest["packets"][1:]:
                harness.record_response(
                    run_dir,
                    entry["packet_id"],
                    _with_packet_echo("照課表跑。", entry["packet_id"]),
                    executor,
                )
            rendered = harness.render_report(harness.report(run_dir))
            self.assertIn("#1", rendered)
            self.assertIn("#2", rendered)

    def test_render_report_keeps_the_single_sample_label_unchanged(self):
        # The overwhelmingly common case -- exactly one sample per packet -- renders
        # with no sample suffix, identical to a run from before sampling existed.
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = self._run(tmp)
            manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
            executor = {"provider": "anthropic", "model": "claude-opus-5"}
            for entry in manifest["packets"]:
                harness.record_response(
                    run_dir,
                    entry["packet_id"],
                    _with_packet_echo("照課表跑。", entry["packet_id"]),
                    executor,
                )
            rendered = harness.render_report(harness.report(run_dir))
            self.assertNotIn("#1", rendered)

    def test_a_legacy_single_answer_response_with_no_sample_key_reports_as_sample_one(self):
        # Simulates a response recorded before sampling existed: no "sample" key at
        # all, only the bare packet_id.json name this format has always used.
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = self._run(tmp)
            manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
            entry = manifest["packets"][0]
            legacy_path = run_dir / "responses" / f"{entry['packet_id']}.json"
            legacy_path.parent.mkdir(parents=True, exist_ok=True)
            legacy_path.write_text(
                json.dumps(
                    {
                        "packet_id": entry["packet_id"],
                        "recorded_at": "2026-01-01T00:00:00+00:00",
                        "packet_sha256": entry["sha256"],
                        "executor": {"provider": "anthropic", "model": "claude-opus-5"},
                        "answer": "舊格式答案，沒有 sample 欄位。",
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            value = harness.report(run_dir)
            row = next(r for r in value["rows"] if r["packet_id"] == entry["packet_id"])
            self.assertEqual("answered", row["status"])
            self.assertEqual([1], [s["sample"] for s in row["samples"]])
            self.assertEqual("舊格式答案，沒有 sample 欄位。", row["samples"][0]["answer"])

    def test_cli_record_response_accepts_a_sample_flag(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = self._run(tmp)
            packet_id = self._first_packet(run_dir)
            answer_path = Path(tmp) / "answer.txt"
            answer_path.write_text(_with_packet_echo("答案", packet_id), encoding="utf-8")
            code = harness.main(
                [
                    "record-response",
                    "--run", str(run_dir),
                    "--packet", packet_id,
                    "--answer-file", str(answer_path),
                    "--provider", "anthropic",
                    "--model", "claude-opus-5",
                    "--sample", "2",
                ]
            )
            self.assertEqual(0, code)
            self.assertTrue((run_dir / "responses" / f"{packet_id}.sample2.json").is_file())

    def test_cli_record_response_without_a_sample_flag_defaults_to_one(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = self._run(tmp)
            packet_id = self._first_packet(run_dir)
            answer_path = Path(tmp) / "answer.txt"
            answer_path.write_text(_with_packet_echo("答案", packet_id), encoding="utf-8")
            code = harness.main(
                [
                    "record-response",
                    "--run", str(run_dir),
                    "--packet", packet_id,
                    "--answer-file", str(answer_path),
                    "--provider", "anthropic",
                    "--model", "claude-opus-5",
                ]
            )
            self.assertEqual(0, code)
            self.assertTrue((run_dir / "responses" / f"{packet_id}.json").is_file())


if __name__ == "__main__":
    unittest.main()
