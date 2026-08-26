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
from unittest import mock

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
                harness.record_response(run_dir, entry["packet_id"], "今天照課表跑。", executor)
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
        self.assertNotIn("overlay_fields", self.suite)
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
                run_dir, packet_id, "答案", {"provider": "anthropic", "model": "claude-opus-5"}
            )
            self.assertEqual(f"{packet_id}.json", path.name)

    def test_a_second_and_third_sample_are_named_explicitly_and_do_not_collide(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = self._run(tmp)
            packet_id = self._first_packet(run_dir)
            executor = {"provider": "anthropic", "model": "claude-opus-5"}
            first = harness.record_response(run_dir, packet_id, "第一次答案", executor, sample=1)
            second = harness.record_response(run_dir, packet_id, "第二次答案", executor, sample=2)
            third = harness.record_response(run_dir, packet_id, "第三次答案", executor, sample=3)
            self.assertEqual(3, len({first, second, third}))
            for path in (first, second, third):
                self.assertTrue(path.is_file())

    def test_naming_a_sample_already_recorded_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = self._run(tmp)
            packet_id = self._first_packet(run_dir)
            executor = {"provider": "anthropic", "model": "claude-opus-5"}
            harness.record_response(run_dir, packet_id, "第一次", executor, sample=2)
            with self.assertRaises(harness.EvalError):
                harness.record_response(run_dir, packet_id, "重寫第二次", executor, sample=2)

    def test_sample_zero_or_negative_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = self._run(tmp)
            packet_id = self._first_packet(run_dir)
            executor = {"provider": "anthropic", "model": "claude-opus-5"}
            with self.assertRaises(harness.EvalError):
                harness.record_response(run_dir, packet_id, "答案", executor, sample=0)

    def test_report_lists_every_sample_of_one_packet_side_by_side(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = self._run(tmp)
            manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
            executor = {"provider": "anthropic", "model": "claude-opus-5"}
            target = manifest["packets"][0]["packet_id"]
            harness.record_response(run_dir, target, "第一次照課表跑。", executor, sample=1)
            harness.record_response(run_dir, target, "第二次照課表跑，用詞不同。", executor, sample=2)
            for entry in manifest["packets"][1:]:
                harness.record_response(run_dir, entry["packet_id"], "照課表跑。", executor)

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
            harness.record_response(run_dir, target, "第一次答案。", executor, sample=1)
            harness.record_response(run_dir, target, "第二次答案。", executor, sample=2)
            for entry in manifest["packets"][1:]:
                harness.record_response(run_dir, entry["packet_id"], "答案。", executor)
            blob = json.dumps(harness.report(run_dir)).lower()
            for word in ("average", "mean", "verdict", "score"):
                self.assertNotIn(word, blob)

    def test_render_report_labels_each_sample_when_there_is_more_than_one(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = self._run(tmp)
            manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
            executor = {"provider": "anthropic", "model": "claude-opus-5"}
            target = manifest["packets"][0]["packet_id"]
            harness.record_response(run_dir, target, "第一次照課表跑。", executor, sample=1)
            harness.record_response(run_dir, target, "第二次照課表跑。", executor, sample=2)
            for entry in manifest["packets"][1:]:
                harness.record_response(run_dir, entry["packet_id"], "照課表跑。", executor)
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
                harness.record_response(run_dir, entry["packet_id"], "照課表跑。", executor)
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
            answer_path.write_text("答案", encoding="utf-8")
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
            answer_path.write_text("答案", encoding="utf-8")
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
