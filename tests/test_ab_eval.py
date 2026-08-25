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

import copy
import json
import tempfile
import unittest
from pathlib import Path

from evals.ab import figures
from evals.ab import harness
from tests import coach_session_scenarios as scenarios_module


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
                    run_dir, packet_id, "答案", {"provider": "anthropic", "model": ""}
                )

    def test_an_empty_answer_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = self._run(tmp)
            with self.assertRaises(harness.EvalError):
                harness.record_response(
                    run_dir,
                    self._first_packet(run_dir),
                    "   \n",
                    {"provider": "anthropic", "model": "claude-opus-5"},
                )

    def test_a_recorded_answer_is_never_written_over(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = self._run(tmp)
            packet_id = self._first_packet(run_dir)
            executor = {"provider": "anthropic", "model": "claude-opus-5"}
            harness.record_response(run_dir, packet_id, "第一次的答案", executor)
            with self.assertRaises(harness.EvalError):
                harness.record_response(run_dir, packet_id, "改過的答案", executor)

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
                    "今天照課表跑。",
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
                    "今天照課表跑。",
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


if __name__ == "__main__":
    unittest.main()
