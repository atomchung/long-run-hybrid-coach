"""Ask one coaching question of three context builds and keep the three answers.

``evals/README.md`` says what a manual run cannot do: *"A manual run cannot be replayed,
so it cannot show whether a change improved anything or only moved it, which is what a
prompt change most needs to prove."* The same sentence is true of a context change, and
issue #240 §3 is one: a `cycle_sessions` row from before the previous week stopped
carrying what its session prescribed, and whether that costs the coach anything is a
question about answers, not about characters.

What this module is, exactly:

* **an A of the same question, three times.** One arm per context build. The question,
  the served instructions, the training reference and the model are held fixed; the only
  thing that varies between two packets is what the tool call handed back.
* **replayable.** Packets are built from committed fixtures, so the same command on the
  same checkout produces byte-identical packets, and every packet is hashed into the
  run's manifest. Two runs are comparable because their packets are the same bytes, not
  because somebody remembers setting them up the same way.
* **not a model caller.** Nothing here imports a client or reaches a network (AGENTS.md:
  the repository must not call an LLM API). A packet is a file. Whoever plays the coach
  answers it out of process and hands the text back to ``record-response``, naming the
  model they used -- and the report refuses to compare arms answered by different models,
  because that comparison would be measuring the model.
* **not a judge.** ``evals/README.md``: *"There is no LLM judge"*, and no weighted total.
  What comes out is the three answers side by side plus the figures each one stated that
  its own context did not carry. The verdict is read by a person.

Where a run lives: outside the repository, like every other eval artifact, under
``$GARMIN_COACH_LOOP_HOME/evals/ab/runs`` or ``~/.local/share/garmin-coach-loop/evals/ab/runs``.
Answers are the athlete-facing text of a real model on a real question and they are not
committed.
"""

from __future__ import annotations

import argparse
import copy
import datetime as dt
import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

from evals.ab import figures as figures_module


ROOT = Path(__file__).resolve().parents[2]
SUITE_PATH = Path(__file__).resolve().parent / "suite.json"
ARMS_DIR = Path(__file__).resolve().parent / "arms"

# The two context fields the arms differ in, and the only two an overlay may replace.
# Naming them here rather than diffing whatever happens to differ is what makes an arm a
# statement about one change: if a later build moves something else, the untouched digest
# below fails rather than the arm quietly becoming a different comparison.
OVERLAY_FIELDS = ("cycle_sessions", "recent_actuals")

# The vocabulary a reviewer records. Same four words as `evals/README.md`, so a verdict
# here and a verdict on a behaviour case mean the same thing.
VERDICTS = ("pass", "partial", "fail", "disputed")

_SAFE_ID = re.compile(r"^[A-Za-z0-9._-]+$")
# One interrogative sentence, in either language the product answers in.
_QUESTION = re.compile(r"[^。．.!?？！\n]*[?？]")
# What an answer says when it is declining to state something it was not handed. Read as
# a count, never as a pass: saying "不確定" about the wrong thing is still wrong. "沒辦法"
# overlaps "沒辦法判斷" on purpose -- an answer carrying both is counted for each, which
# double-counts one decline rather than missing it. That is the right side to err on: a
# reviewer reads this as a count, not a score, so a phrase that broad but unambiguous is
# worth more here than a narrower list that stays silent on a real refusal.
_UNCERTAINTY = (
    "不確定", "沒有紀錄", "沒有記錄", "查不到", "沒有資料", "無法確認", "不知道",
    "未知", "沒辦法判斷", "沒辦法", "回答不了", "只查得到", "無從", "unknown",
    "no record", "not recorded", "cannot tell", "can't tell", "unclear",
)


class EvalError(RuntimeError):
    """Anything that should stop a run with a message rather than a traceback."""


# -- small shared helpers -----------------------------------------------------------


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(
        "utf-8"
    )


def _sha(value: Any) -> str:
    payload = value if isinstance(value, bytes) else _canonical(value)
    return hashlib.sha256(payload).hexdigest()


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise EvalError(f"no such file: {path}") from exc
    except json.JSONDecodeError as exc:
        raise EvalError(f"{path} is not readable JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise EvalError(f"{path} is not a JSON object")
    return value


def _dump(value: Any) -> str:
    return json.dumps(value, indent=2, ensure_ascii=False, sort_keys=True) + "\n"


def _write_new(path: Path, value: Any) -> Path:
    """Write once. A second write to the same path is an error, not an overwrite.

    An answer that can be edited after it was recorded is not evidence of anything.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("x", encoding="utf-8") as handle:
            handle.write(_dump(value))
    except FileExistsError as exc:
        raise EvalError(f"refusing to overwrite a recorded eval artifact: {path}") from exc
    return path


def _safe_id(value: str, field: str) -> str:
    if not isinstance(value, str) or not _SAFE_ID.match(value):
        raise EvalError(f"{field} must match [A-Za-z0-9._-]+, got {value!r}")
    return value


def default_run_root() -> Path:
    home = os.environ.get("GARMIN_COACH_LOOP_HOME")
    base = Path(home) if home else Path.home() / ".local" / "share" / "garmin-coach-loop"
    return base / "evals" / "ab" / "runs"


def _require_external(path: Path) -> Path:
    """A run holds model answers, so it stays out of the repository."""
    resolved = path.expanduser().resolve()
    if resolved == ROOT or ROOT in resolved.parents:
        raise EvalError(f"eval runs must be written outside the repository: {resolved}")
    return resolved


# -- the suite ----------------------------------------------------------------------


def load_suite(path: Path = SUITE_PATH) -> dict[str, Any]:
    """The suite, checked for the shape every command below assumes."""
    suite = _read_json(path)
    for field in ("suite_id", "suite_version", "arms", "dimensions", "turns"):
        if field not in suite:
            raise EvalError(f"suite is missing {field}")
    arm_ids: list[str] = []
    live = 0
    for arm in suite["arms"]:
        arm_ids.append(_safe_id(arm.get("arm_id", ""), "arm_id"))
        if arm.get("source") not in ("frozen", "live"):
            raise EvalError(f"arm {arm['arm_id']} must be source frozen or live")
        live += arm["source"] == "live"
        if not str(arm.get("summary", "")).strip():
            raise EvalError(f"arm {arm['arm_id']} has no summary")
    if len(set(arm_ids)) != len(arm_ids):
        raise EvalError("two arms share an arm_id")
    if live != 1:
        raise EvalError("exactly one arm is the live checkout")
    if not suite["dimensions"]:
        raise EvalError("a suite with no dimensions grades nothing")
    if "overlay_fields" in suite:
        declared = suite["overlay_fields"]
        if not isinstance(declared, list) or not declared:
            raise EvalError("overlay_fields must be a non-empty list of field names")
        for name in declared:
            _safe_id(name, "overlay_fields")
    turn_ids: list[str] = []
    for turn in suite["turns"]:
        turn_ids.append(_safe_id(turn.get("turn_id", ""), "turn_id"))
        for field in ("scenario", "mode", "covers", "question", "target_session_ids", "why"):
            if not turn.get(field):
                raise EvalError(f"turn {turn['turn_id']} is missing {field}")
    if len(set(turn_ids)) != len(turn_ids):
        raise EvalError("two turns share a turn_id")
    return suite


def live_arm_id(suite: dict[str, Any]) -> str:
    return next(arm["arm_id"] for arm in suite["arms"] if arm["source"] == "live")


def overlay_fields(suite: dict[str, Any]) -> tuple[str, ...]:
    """The context fields a frozen arm in this suite is allowed to overlay.

    Suite-declared when the suite says so -- a later suite compares a different field,
    or just one (``strength_execution`` rather than the pair below), without this
    module changing. Silent on the question, a suite gets exactly the two fields every
    arm before this option existed was captured against, so an old suite -- and the
    arms captured against it -- overlay exactly what they always did.
    """
    declared = suite.get("overlay_fields")
    return tuple(declared) if declared is not None else OVERLAY_FIELDS


# -- the arms -----------------------------------------------------------------------


def _scenario_response(name: str) -> dict[str, Any]:
    """What ``startCoachSession`` hands back for one scenario, from this checkout.

    Imported rather than copied: ``tests/coach_session_scenarios.py`` is where these
    reads are defined, and a second copy of a scenario is the copy nobody edits.
    """
    from tests import coach_session_scenarios as scenarios_module

    scenario = next(
        (item for item in scenarios_module.scenarios() if item.name == name), None
    )
    if scenario is None:
        raise EvalError(f"no scenario named {name!r} in tests/coach_session_scenarios.py")
    snapshot = scenarios_module.run(scenario)
    if snapshot["response"] is None:
        raise EvalError(f"scenario {name!r} ends in a blocked build and has no context")
    return snapshot["response"]


def _mirrored(response: dict[str, Any], fields: tuple[str, ...]) -> tuple[str, ...]:
    """The overlay fields this response also carries beside the context, not only in it.

    ``startCoachSession`` hands ``unknowns`` back twice -- once inside ``context`` and
    once at the top level -- and the two are the same list. A suite overlaying that
    field cannot move only the inner copy: the packet would state two different things
    about what the coach does not know, and the untouched digest would fail on the copy
    left behind, which reads as "this checkout changed something else" when nothing
    changed. So a mirror travels with the field it mirrors.

    Only a field the context actually owns can have a mirror here. ``plan_state`` and
    ``validation`` sit beside the context rather than inside it and are nobody's
    overlay; naming one would be a different feature, and this is not it.
    """
    context = response.get("context")
    if not isinstance(context, dict):
        return ()
    return tuple(field for field in fields if field in context and field in response)


def _untouched(response: dict[str, Any], fields: tuple[str, ...] = OVERLAY_FIELDS) -> dict[str, Any]:
    """The response with the overlaid fields removed -- everything an arm shares."""
    rest = copy.deepcopy(response)
    for field in _mirrored(rest, fields):
        rest.pop(field, None)
    context = rest.get("context")
    if isinstance(context, dict):
        for field in fields:
            context.pop(field, None)
    return rest


def overlay_path(arm_id: str, scenario: str) -> Path:
    return ARMS_DIR / arm_id / f"{scenario}.json"


def arm_response(
    arm_id: str, scenario: str, suite: dict[str, Any], *, live: dict[str, Any] | None = None
) -> dict[str, Any]:
    """The response one arm hands the coach for one scenario.

    The live arm is whatever this checkout builds. A frozen arm is that same response
    with its recorded overlay fields put back -- which is only honest while everything
    *outside* those fields still matches what the arm was captured beside, so the
    recorded digest is checked here rather than in a test that might not be run.
    """
    response = copy.deepcopy(live) if live is not None else _scenario_response(scenario)
    if arm_id == live_arm_id(suite):
        return response
    fields = overlay_fields(suite)
    record = _read_json(overlay_path(arm_id, scenario))
    untouched = _sha(_untouched(response, fields))
    if record.get("untouched_sha256") != untouched:
        raise EvalError(
            f"arm {arm_id} / {scenario}: this checkout changed something outside "
            f"{', '.join(fields)}, so the frozen arm is no longer that commit's "
            f"answer with one field swapped. Re-capture the arm, or widen this suite's "
            f"overlay_fields and say why."
        )
    context = response.get("context")
    if not isinstance(context, dict):
        raise EvalError(f"scenario {scenario} has no context to overlay")
    mirrored = _mirrored(response, fields)
    for field in fields:
        if field in record["overlay"]:
            value = copy.deepcopy(record["overlay"][field])
            context[field] = value
            if field in mirrored:
                response[field] = copy.deepcopy(value)
        else:
            context.pop(field, None)
            if field in mirrored:
                response.pop(field, None)
    return response


def capture_arm(arm_id: str, commit: str | None, note: str, suite: dict[str, Any]) -> list[Path]:
    """Freeze one arm's overlay fields, as this working tree currently builds them.

    Run this from a checkout of the commit the arm names -- normally a throwaway
    ``git worktree`` at that commit with this directory's scenario definitions copied in,
    which is the whole procedure and is written out in ``README.md``. The capture is a
    developer action on a full clone; the test suite only ever reads the result, so CI's
    shallow checkout never needs the history.
    """
    written: list[Path] = []
    fields = overlay_fields(suite)
    for scenario in sorted({turn["scenario"] for turn in suite["turns"]}):
        response = _scenario_response(scenario)
        context = response.get("context") or {}
        record = {
            "arm": arm_id,
            "scenario": scenario,
            "commit": commit,
            "note": note,
            "captured_at_head": _git_sha(),
            "untouched_sha256": _sha(_untouched(response, fields)),
            "overlay": {
                field: copy.deepcopy(context[field])
                for field in fields
                if field in context
            },
        }
        path = overlay_path(arm_id, scenario)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(_dump(record), encoding="utf-8")
        written.append(path)
    return written


def refresh_arm_digest(arm_id: str, suite: dict[str, Any]) -> list[dict[str, Any]]:
    """Recompute one arm's ``untouched_sha256`` against what this checkout builds now.

    ``capture_arm`` freezes a checkout of the arm's own commit, so it cannot repair a
    digest a new top-level context key broke: that checkout has no such key to hash, and
    replaying the capture there reproduces the same failing file (README.md, "When a new
    top-level context key stops every arm" -- verified for #28, where capturing
    ``prose-on-every-row`` at ``c73b030`` rewrote all seven files byte for byte and left
    them still failing). This checkout is the one that can see the new key, so it is the
    one that has to compute the digest -- the overlay itself, the old commit's answer,
    never has a reason to move.

    Every field but the digest is read back exactly as written and, when it needs no
    change, never rewritten -- so an arm already honest against this build is untouched
    down to the byte. That is the manual repair PR #308 did by hand, mechanized (#309).
    """
    fields = overlay_fields(suite)
    results: list[dict[str, Any]] = []
    for scenario in sorted({turn["scenario"] for turn in suite["turns"]}):
        path = overlay_path(arm_id, scenario)
        record = _read_json(path)
        fresh = _sha(_untouched(_scenario_response(scenario), fields))
        changed = record.get("untouched_sha256") != fresh
        if changed:
            record["untouched_sha256"] = fresh
            path.write_text(_dump(record), encoding="utf-8")
        results.append(
            {"arm": arm_id, "scenario": scenario, "path": path, "changed": changed}
        )
    return results


def refresh_all_arm_digests(suite: dict[str, Any]) -> list[dict[str, Any]]:
    """``refresh_arm_digest``, for every frozen arm this suite declares."""
    results: list[dict[str, Any]] = []
    for arm in suite["arms"]:
        if arm["source"] != "frozen":
            continue
        results.extend(refresh_arm_digest(arm["arm_id"], suite))
    return results


def _git_sha() -> str | None:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, check=True, capture_output=True
        )
    except (OSError, subprocess.CalledProcessError):
        return None
    return result.stdout.decode().strip() or None


# -- packets ------------------------------------------------------------------------


def _materials() -> dict[str, str]:
    """The two texts a hosted coaching turn is given before any tool result.

    Read off the package rather than restated, so a packet is what the product actually
    serves and an edit to either text shows up as a changed packet hash.
    """
    from garmin_coach_loop.orchestration import training_judgment

    return {
        "orchestration": (ROOT / "garmin_coach_loop" / "orchestration.md").read_text(
            encoding="utf-8"
        ),
        "training_judgment": training_judgment(),
    }


PACKET_INSTRUCTIONS = (
    "Answer as the athlete's coach, in the language they asked in.",
    "Use only this packet. Do not open the repository, the suite, the other packets, "
    "or any earlier run.",
    "start_coach_session below is the tool result you would have received. There is no "
    "second call to make and no further evidence coming.",
    "Do not write to PlanState, deliver a workout, or claim either happened.",
    "Return the answer text only -- no scoring, no notes about this exercise.",
)


def build_packet(
    *, run_id: str, arm_id: str, turn: dict[str, Any], response: dict[str, Any]
) -> dict[str, Any]:
    """One coach turn, with nothing in it that says which arm it is.

    The arm is deliberately absent: a packet that names its own build invites an answer
    about the build. The mapping lives in the run manifest, which whoever answers is
    asked not to read.
    """
    packet_id = _sha(f"{run_id}:{arm_id}:{turn['turn_id']}".encode("utf-8"))[:12]
    return {
        "packet_id": packet_id,
        "asked_at": response.get("context", {}).get("as_of"),
        "materials": _materials(),
        "instructions": list(PACKET_INSTRUCTIONS),
        "athlete_says": turn["question"],
        "start_coach_session": response,
    }


# -- a run --------------------------------------------------------------------------


def create_run(
    *,
    run_id: str,
    run_root: Path | None = None,
    suite_path: Path = SUITE_PATH,
    turn_ids: list[str] | None = None,
) -> Path:
    """Write every packet this suite asks for, and the manifest that maps them back."""
    suite = load_suite(suite_path)
    _safe_id(run_id, "run_id")
    root = _require_external(run_root or default_run_root())
    run_dir = root / run_id
    if run_dir.exists():
        raise EvalError(f"run {run_id} already exists at {run_dir}")

    turns = suite["turns"]
    if turn_ids is not None:
        wanted = set(turn_ids)
        turns = [turn for turn in turns if turn["turn_id"] in wanted]
        missing = wanted - {turn["turn_id"] for turn in turns}
        if missing:
            raise EvalError(f"no such turn: {', '.join(sorted(missing))}")
    if not turns:
        raise EvalError("a run with no turns asks nothing")

    live_id = live_arm_id(suite)
    live_by_scenario = {
        scenario: _scenario_response(scenario)
        for scenario in sorted({turn["scenario"] for turn in turns})
    }

    packets: list[dict[str, Any]] = []
    for turn in turns:
        for arm in suite["arms"]:
            response = arm_response(
                arm["arm_id"], turn["scenario"], suite, live=live_by_scenario[turn["scenario"]]
            )
            packet = build_packet(
                run_id=run_id, arm_id=arm["arm_id"], turn=turn, response=response
            )
            path = run_dir / "packets" / f"{packet['packet_id']}.json"
            _write_new(path, packet)
            packets.append(
                {
                    "packet_id": packet["packet_id"],
                    "arm": arm["arm_id"],
                    "turn_id": turn["turn_id"],
                    "scenario": turn["scenario"],
                    "path": str(path.relative_to(run_dir)),
                    "sha256": _sha(packet),
                    "context_characters": len(
                        figures_module.json_text(response.get("context"))
                    ),
                }
            )

    identical = _identical_arms(suite, turns, live_by_scenario, live_id)
    manifest = {
        "run_id": run_id,
        "created_at": _utc_now(),
        "repo_sha": _git_sha(),
        "suite": {
            "suite_id": suite["suite_id"],
            "suite_version": suite["suite_version"],
            "sha256": _sha(suite),
        },
        "arms": suite["arms"],
        "turn_ids": [turn["turn_id"] for turn in turns],
        "packets": packets,
        # Reported, never asserted. Before the builder changes, the live arm is one of
        # the frozen ones and every difference this run reports is noise -- which is
        # worth knowing before reading the report, and worth keeping in the record
        # afterwards as the run that showed the instrument reads zero.
        "arms_identical_to_live": identical,
    }
    _write_new(run_dir / "manifest.json", manifest)
    _write_new(run_dir / "suite.json", suite)
    return run_dir


def _identical_arms(
    suite: dict[str, Any],
    turns: list[dict[str, Any]],
    live_by_scenario: dict[str, dict[str, Any]],
    live_id: str,
) -> list[str]:
    same: list[str] = []
    for arm in suite["arms"]:
        if arm["arm_id"] == live_id:
            continue
        scenarios = sorted({turn["scenario"] for turn in turns})
        if all(
            _sha(arm_response(arm["arm_id"], name, suite, live=live_by_scenario[name]))
            == _sha(live_by_scenario[name])
            for name in scenarios
        ):
            same.append(arm["arm_id"])
    return same


def _utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


def load_run(run_dir: Path) -> tuple[Path, dict[str, Any], dict[str, Any]]:
    resolved = _require_external(run_dir)
    manifest = _read_json(resolved / "manifest.json")
    suite = _read_json(resolved / "suite.json")
    if _sha(suite) != manifest["suite"]["sha256"]:
        raise EvalError(f"{resolved}/suite.json no longer hashes to what the run recorded")
    return resolved, manifest, suite


def _response_path(resolved: Path, packet_id: str, sample: int) -> Path:
    """Where one sample of one packet's answer is stored.

    Sample 1 keeps the bare name every run used before repeated sampling existed, so
    the common call -- one answer, no ``sample`` named -- writes exactly where it
    always did. A higher sample gets its own file beside it.
    """
    if sample == 1:
        return resolved / "responses" / f"{packet_id}.json"
    return resolved / "responses" / f"{packet_id}.sample{sample}.json"


def _recorded_samples(resolved: Path, packet_id: str) -> list[tuple[int, Path]]:
    """Every sample already recorded for one packet, as ``(sample, path)``, ascending.

    A run from before repeated sampling existed recorded one answer, at the bare path
    sample 1 still uses, with no sample number anywhere in it -- so it is found here
    as exactly one sample, the same shape a new run's first sample takes.
    """
    responses_dir = resolved / "responses"
    found: list[tuple[int, Path]] = []
    legacy = responses_dir / f"{packet_id}.json"
    if legacy.is_file():
        found.append((1, legacy))
    if responses_dir.is_dir():
        prefix, suffix = f"{packet_id}.sample", ".json"
        for path in responses_dir.glob(f"{prefix}*{suffix}"):
            digits = path.name[len(prefix) : -len(suffix)]
            if digits.isdigit():
                found.append((int(digits), path))
    return sorted(found, key=lambda item: item[0])


def record_response(
    run_dir: Path,
    packet_id: str,
    answer: str,
    executor: dict[str, Any],
    *,
    sample: int = 1,
) -> Path:
    """File one answer against the packet it answered, and against the model that gave it.

    ``sample`` distinguishes repeated answers to the same packet -- the same model
    asked again, to read how much its own wording moves on repetition alone (the entry
    point issue #86 asked for). It defaults to 1, so the call every run before this
    option existed made -- once per packet, no ``sample`` named -- still writes exactly
    one file and still refuses a second call with the same arguments: the write-once
    guarantee now names the sample it protects, rather than assuming there is only one.
    """
    resolved, manifest, _ = load_run(run_dir)
    entry = next(
        (item for item in manifest["packets"] if item["packet_id"] == packet_id), None
    )
    if entry is None:
        raise EvalError(f"run {manifest['run_id']} has no packet {packet_id}")
    packet = _read_json(resolved / entry["path"])
    if _sha(packet) != entry["sha256"]:
        raise EvalError(f"packet {packet_id} on disk is not the one this run recorded")
    if not answer.strip():
        raise EvalError("an empty answer records nothing")
    for field in ("provider", "model"):
        if not str(executor.get(field, "")).strip():
            raise EvalError(f"executor.{field} is required -- an unnamed model is not an arm")
    if sample < 1:
        raise EvalError("sample must be 1 or greater")
    return _write_new(
        _response_path(resolved, packet_id, sample),
        {
            "packet_id": packet_id,
            "sample": sample,
            "recorded_at": _utc_now(),
            "packet_sha256": entry["sha256"],
            "executor": executor,
            "answer": answer,
        },
    )


# -- what a recorded answer measurably did -------------------------------------------


# The arm whose rows carry every session's prescription, whatever week it sat in. It is
# the reference for "what this session actually asked for" -- committed data, so a scored
# answer is never measured against the arm it came from.
REFERENCE_ARM = "prose-on-every-row"


def prescribed_texts(scenario: str, *, live: dict[str, Any] | None = None) -> dict[str, str]:
    """What every session of one scenario prescribed, whichever week it sat in.

    Two sources, because a cycle has two kinds of session by the time it is reviewed. The
    reference arm's rows carry the elapsed ones -- that arm exists precisely because it
    drops nothing -- and the stored week carries the ones whose day has not passed, which
    are in no cycle record yet.
    """
    texts: dict[str, str] = {}
    path = overlay_path(REFERENCE_ARM, scenario)
    if path.is_file():
        for row in _read_json(path)["overlay"].get("cycle_sessions") or []:
            text = row.get("prescription")
            if isinstance(text, str) and text:
                texts[row["session_id"]] = text
    response = live if live is not None else _scenario_response(scenario)
    week = ((response.get("plan_state") or {}).get("current_plan") or {}).get("week") or {}
    for session in week.get("sessions") or []:
        text = session.get("prescription")
        if isinstance(text, str) and text:
            texts.setdefault(session["session_id"], text)
    return texts


def signals(
    *, answer: str, packet: dict[str, Any], turn: dict[str, Any]
) -> dict[str, Any]:
    """Everything about one answer that is a fact rather than a judgment.

    Six counts, all of them readable in one line, none of them a score. What they are
    for, in the order a reviewer reads them:

    ``figures_not_in_the_context``  the fabrication check, and the reason this file
                                    exists. Derived figures land here too -- read the
                                    list, do not total it.
    ``prescribed_figures_stated``   how much of what the session actually asked for the
                                    answer managed to say, against how much the arm's
                                    own context carried. The two together are the A/B.
    ``questions_asked``             an answer that cannot read something asks for it.
    ``uncertainty_markers``         or says it cannot. Both are honest; both are also
                                    the coach doing less than the other arm did.
    """
    supported = figures_module.supported_figures(
        packet.get("start_coach_session"), turn["question"]
    )
    texts = prescribed_texts(turn["scenario"], live=packet.get("start_coach_session"))
    prescribed = set()
    for session_id in turn["target_session_ids"]:
        prescribed |= figures_module.figures_in_text(texts.get(session_id, ""))
    # "In this arm" is measured against the whole packet, not against the cycle row
    # alone. A figure the row dropped is still readable if the stored week, a later
    # week's repeat or movement_history states it -- and a count that ignored those
    # would report a hole where the coach has none, which is the more damaging error
    # of the two.
    in_arm = prescribed & supported
    stated = figures_module.figures_in_text(answer)
    return {
        "answer_characters": len(answer),
        "figures_not_in_the_context": figures_module.unsupported_figures(answer, supported),
        "prescribed_figures_total": len(prescribed),
        "prescribed_figures_in_this_arm": len(in_arm),
        "prescribed_figures_stated": len(prescribed & stated),
        "questions_asked": len(_QUESTION.findall(answer)),
        "uncertainty_markers": sum(marker in answer for marker in _UNCERTAINTY),
    }


def report(run_dir: Path) -> dict[str, Any]:
    """The answers to each question, side by side, with what each one measurably did.

    A packet with more than one recorded answer reports every sample it has, side by
    side under the same row -- never averaged and never scored down to a verdict. A
    reviewer reads the spread across samples the same way they read the spread across
    arms: by looking at it.
    """
    resolved, manifest, suite = load_run(run_dir)
    turns = {turn["turn_id"]: turn for turn in suite["turns"]}
    executors: set[str] = set()
    rows: list[dict[str, Any]] = []
    for entry in manifest["packets"]:
        recorded_samples = _recorded_samples(resolved, entry["packet_id"])
        row: dict[str, Any] = {
            "turn_id": entry["turn_id"],
            "arm": entry["arm"],
            "packet_id": entry["packet_id"],
            "context_characters": entry["context_characters"],
        }
        if not recorded_samples:
            row["status"] = "unanswered"
            rows.append(row)
            continue
        packet = _read_json(resolved / entry["path"])
        turn = turns[entry["turn_id"]]
        row["status"] = "answered"
        row["samples"] = []
        for sample_index, answer_path in recorded_samples:
            recorded = _read_json(answer_path)
            executor_label = (
                f"{recorded['executor']['provider']}/{recorded['executor']['model']}"
            )
            executors.add(executor_label)
            row["samples"].append(
                {
                    "sample": sample_index,
                    "executor": executor_label,
                    "answer": recorded["answer"],
                    "signals": signals(answer=recorded["answer"], packet=packet, turn=turn),
                }
            )
        rows.append(row)

    answered = [row for row in rows if row["status"] == "answered"]
    comparable = len(executors) <= 1
    return {
        "run_id": manifest["run_id"],
        "suite": manifest["suite"],
        "arms": manifest["arms"],
        "arms_identical_to_live": manifest.get("arms_identical_to_live", []),
        "executors": sorted(executors),
        # The A in A/B is one model. Two models across the arms measures the models.
        "comparable": comparable,
        "not_comparable_because": (
            None if comparable else "arms were answered by more than one model"
        ),
        "answered": len(answered),
        "of": len(rows),
        "rows": rows,
    }


def render_report(value: dict[str, Any]) -> str:
    """The report as something to read in a terminal rather than to parse."""
    lines = [
        f"run {value['run_id']}  suite {value['suite']['suite_id']} "
        f"v{value['suite']['suite_version']}",
        f"answered {value['answered']} of {value['of']}"
        + (f"  by {', '.join(value['executors'])}" if value["executors"] else ""),
    ]
    if not value["comparable"]:
        lines.append(f"NOT COMPARABLE: {value['not_comparable_because']}")
    if value["arms_identical_to_live"]:
        lines.append(
            "identical to the live checkout at build time: "
            + ", ".join(value["arms_identical_to_live"])
        )
    by_turn: dict[str, list[dict[str, Any]]] = {}
    for row in value["rows"]:
        by_turn.setdefault(row["turn_id"], []).append(row)
    for turn_id, turn_rows in by_turn.items():
        lines.append("")
        lines.append(turn_id)
        for row in turn_rows:
            if row["status"] != "answered":
                lines.append(f"  {row['arm']:24s} unanswered  packet {row['packet_id']}")
                continue
            multi_sample = len(row["samples"]) > 1
            for sample in row["samples"]:
                signal = sample["signals"]
                label = f"{row['arm']} #{sample['sample']}" if multi_sample else row["arm"]
                lines.append(
                    f"  {label:24s} "
                    f"context {row['context_characters']:6d}  "
                    f"prescribed {signal['prescribed_figures_stated']}"
                    f"/{signal['prescribed_figures_in_this_arm']}"
                    f"/{signal['prescribed_figures_total']} stated/in-arm/total  "
                    f"unsupported {len(signal['figures_not_in_the_context'])}  "
                    f"asks {signal['questions_asked']}  "
                    f"unknowns {signal['uncertainty_markers']}"
                )
                if signal["figures_not_in_the_context"]:
                    lines.append(
                        "    figures the context did not carry: "
                        + ", ".join(signal["figures_not_in_the_context"])
                    )
    return "\n".join(lines)


# -- command line --------------------------------------------------------------------


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=(__doc__ or "").strip().splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)

    create = sub.add_parser("create-run", help="write one packet per arm per turn")
    create.add_argument("--run-id", required=True)
    create.add_argument("--run-root", default=None)
    create.add_argument(
        "--suite", default=None, help="path to a suite JSON file; defaults to evals/ab/suite.json"
    )
    create.add_argument("--turn", action="append", dest="turns", default=None)

    record = sub.add_parser("record-response", help="file one answer against its packet")
    record.add_argument("--run", required=True)
    record.add_argument("--packet", required=True)
    record.add_argument("--answer-file", required=True)
    record.add_argument("--provider", required=True)
    record.add_argument("--model", required=True)
    record.add_argument("--agent-id", default=None)
    record.add_argument(
        "--sample",
        type=int,
        default=1,
        help="which attempt at this packet this is; defaults to 1",
    )

    shown = sub.add_parser("report", help="the answers side by side")
    shown.add_argument("--run", required=True)
    shown.add_argument("--json", action="store_true")

    captured = sub.add_parser(
        "capture-arm", help="freeze this working tree's build as one arm (see README)"
    )
    captured.add_argument(
        "--arm", default=None, help="required unless --refresh-digest is given with none"
    )
    captured.add_argument("--commit", default=None)
    captured.add_argument("--note", default="")
    captured.add_argument(
        "--suite", default=None, help="path to a suite JSON file; defaults to evals/ab/suite.json"
    )
    captured.add_argument(
        "--refresh-digest",
        action="store_true",
        help=(
            "leave the overlay untouched and only recompute untouched_sha256 against "
            "this checkout's current build -- the repair a new top-level context key "
            "needs (README.md). Refreshes --arm if given, or every frozen arm in the "
            "suite if not; takes neither --commit nor --note."
        ),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "create-run":
            run_dir = create_run(
                run_id=args.run_id,
                run_root=Path(args.run_root) if args.run_root else None,
                suite_path=Path(args.suite) if args.suite else SUITE_PATH,
                turn_ids=args.turns,
            )
            print(f"wrote {run_dir}")
            print("Answer each file in packets/ and record it with record-response.")
        elif args.command == "record-response":
            path = record_response(
                Path(args.run),
                args.packet,
                Path(args.answer_file).read_text(encoding="utf-8"),
                {
                    "provider": args.provider,
                    "model": args.model,
                    "agent_id": args.agent_id,
                },
                sample=args.sample,
            )
            print(f"wrote {path}")
        elif args.command == "report":
            value = report(Path(args.run))
            print(json.dumps(value, ensure_ascii=False, indent=2) if args.json else render_report(value))
        elif args.command == "capture-arm":
            suite_path = Path(args.suite) if args.suite else SUITE_PATH
            suite = load_suite(suite_path)
            if args.refresh_digest:
                if args.commit is not None or args.note:
                    raise EvalError(
                        "--refresh-digest recomputes untouched_sha256 only -- it takes "
                        "neither --commit nor --note"
                    )
                results = (
                    refresh_arm_digest(args.arm, suite)
                    if args.arm
                    else refresh_all_arm_digests(suite)
                )
                for result in results:
                    status = "refreshed" if result["changed"] else "unchanged"
                    print(f"{status} {result['path'].relative_to(ROOT)}")
            else:
                if not args.arm:
                    raise EvalError("--arm is required unless --refresh-digest is given")
                for path in capture_arm(args.arm, args.commit, args.note, suite):
                    print(f"wrote {path.relative_to(ROOT)}")
    except EvalError as exc:
        print(json.dumps({"status": "blocked", "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
