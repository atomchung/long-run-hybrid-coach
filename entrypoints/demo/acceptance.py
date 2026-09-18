"""The one command that proves this demo works against the real model.

Everything else in this directory is held by tests that never reach a network. What no
test here can answer is whether `gpt-5.6-luna` actually produces a usable answer from this
fixture and this prompt, and whether the request shape this repository built is the one the
Responses API accepts. That question needs a credential and a real call, so it lives in a
command an operator runs deliberately:

    OPENAI_API_KEY=... python3 -m entrypoints.demo.acceptance

That runs the three committed acceptance turns in three separate conversations, and then
the committed three-turn conversation in one, in this process, against the live API.
`--base-url https://demo-api.paceandstaystrong.com` runs the same turns over HTTP against a
deployed service instead, which is the version that also proves CORS, the platform port and
the deploy.

The conversation is the half a separate turn cannot prove. Its second and third turns name
something said in an earlier one -- "the second one", "the first option" -- and carry a
constraint that arrives mid-conversation, so a deployment that lost the history answers them
by asking which option was meant. That is what a visitor met in public, and it is what the
last two checks below are for.

## What it reports, and what it does not

Per turn: whether it completed, how long it took, how many model rounds it used, which
demo acts ran, and a short list of **checks**. Two of those checks are real and two are
hints, and the report says which is which:

*Real*: the turn returned an answer at all; no error code came back; and the reply does not
claim to have saved, delivered or scheduled anything -- a demo that says it updated a
calendar is wrong in a way that matters, and a substring is enough to catch it. So is a
reply naming a score this product does not have. So is a reply that says it cannot see what
was said earlier, or asks which option was meant: that sentence is only ever true of a
conversation this service has lost. And, in the conversation, the turn number the service
answers with -- turn two answering as turn one is a replaced conversation, whatever the
words say.

*A hint*: whether the words that a good answer to this particular question would almost
certainly contain are present. That is a prompt for the operator to read the transcript,
never a verdict on coaching quality. Nothing in this repository grades a coaching answer,
and this file does not become the first thing that does.

The transcripts are written out beside the summary. They are answers about a committed
synthetic athlete, so there is nothing private in them; the credential is never read into
the report, and the report is assembled from this service's own fields rather than from the
provider's response body.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
import tempfile
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any

from . import fixture, model as model_module
from .config import PUBLIC_ENDPOINT, RESPOND_PATH, from_environment
from .service import DemoRequestError, DemoService


# A reply that says any of these has claimed an act the demo does not have. Checked as a
# refusal rather than as a style note: this is the one wrong answer that would mislead a
# visitor about what happened to a plan.
CLAIMED_WRITE = (
    "i've saved",
    "i have saved",
    "i've applied",
    "i have applied",
    "i've updated your calendar",
    "saved to your calendar",
    "added to your calendar",
    "i've scheduled",
    "i have scheduled",
    "i've delivered",
    "has been delivered",
)

# A reply that has lost the conversation it is in. Every phrase here is about the
# conversation rather than about evidence, which is the distinction that matters: "I cannot
# see per-segment heart rate" is a correct answer about a gap in the fixture, while "I cannot
# see what you are referring to" is a session that was replaced. Only the second kind is
# listed, and the service's own no-answer fallback is listed with them because a visitor
# reads it the same way.
FORGOT_THE_CONVERSATION = (
    "earlier in this conversation",
    "this conversation does not",
    "this conversation doesn't",
    "i don't have the previous",
    "i do not have the previous",
    "i don't see the previous",
    "no record of the previous",
    "which option you mean",
    "which option you're referring",
    "which option you are referring",
    "which plan you mean",
    "which one you mean",
    "paste the",
    "repeat the options",
    "remind me what",
    "i could not put that into an answer",
)

# Quantities this product does not define. Naming one is inventing it (issue #472).
INVENTED_SCORE = (
    "running score",
    "strength score",
    "recovery score",
    "readiness score",
    "readiness index",
    "pareto",
)

# Words a good answer to each turn would almost certainly use. A hint for the operator
# reading the transcript, never a grade -- see the module note.
HINTS: dict[str, tuple[str, ...]] = {
    "allocation-under-a-time-budget": ("preserve", "sacrific", "uncertain"),
    "what-changed-and-what-is-unproven": ("unproven", "measurement"),
    "a-day-disappears": ("preserve", "give up"),
    "two-ways-to-adjust": ("option",),
    # Whether the shorter Saturday was carried is a judgment about a coaching answer, so it
    # stays a hint the operator reads the transcript for. What is not a judgment -- that the
    # service answered inside the same conversation -- is a hard check below.
    "the-second-one-and-a-shorter-saturday": ("saturday", "30"),
    "what-the-first-one-would-have-kept": ("saturday",),
}


def _check(name: str, passed: bool, detail: str, *, kind: str) -> dict[str, Any]:
    return {"check": name, "kind": kind, "passed": passed, "detail": detail}


def _grade(
    prompt: dict[str, Any],
    reply: str,
    *,
    expected_turn: int | None = None,
    reported_turn: int | None = None,
) -> list[dict[str, Any]]:
    lowered = reply.lower()
    claimed = [phrase for phrase in CLAIMED_WRITE if phrase in lowered]
    invented = [phrase for phrase in INVENTED_SCORE if phrase in lowered]
    forgot = [phrase for phrase in FORGOT_THE_CONVERSATION if phrase in lowered]
    missing = [word for word in HINTS.get(prompt["id"], ()) if word not in lowered]
    checks = [
        _check("answered", bool(reply.strip()), f"{len(reply)} characters", kind="hard"),
        _check(
            "claims_no_write",
            not claimed,
            "no claimed write" if not claimed else f"claimed: {', '.join(claimed)}",
            kind="hard",
        ),
        _check(
            "invents_no_score",
            not invented,
            "no invented quantity" if not invented else f"named: {', '.join(invented)}",
            kind="hard",
        ),
        _check(
            "remembers_the_conversation",
            not forgot,
            "no lost-conversation wording" if not forgot else f"said: {', '.join(forgot)}",
            kind="hard",
        ),
        _check(
            "reads_like_the_expected_answer",
            not missing,
            "expected wording present" if not missing else f"absent: {', '.join(missing)}",
            kind="hint",
        ),
    ]
    if expected_turn is not None:
        # The service's own count of the conversation it answered in. A second turn that
        # comes back as turn one was answered against an empty history, however well the
        # words happen to read -- and the words can read well, because the fixture alone
        # supports a plausible answer to almost any of these.
        checks.append(
            _check(
                "continued_the_same_conversation",
                reported_turn == expected_turn,
                f"answered as turn {reported_turn}, expected {expected_turn}",
                kind="hard",
            )
        )
    return checks


class _InProcess:
    """Runs the turns through ``DemoService`` directly, on the real model client."""

    def __init__(self, service: DemoService) -> None:
        self._service = service

    @property
    def target(self) -> str:
        return f"in-process ({model_module.MODEL})"

    def ask(self, session_id: str, message: str) -> dict[str, Any]:
        try:
            reply = self._service.respond(
                {"session_id": session_id, "message": message}, client_key="acceptance"
            )
        except DemoRequestError as error:
            return {"ok": False, "status": error.status, "code": error.code,
                    "message": error.message}
        return {"ok": True, "status": reply.status, "reply": reply.body["reply"],
                "turn": reply.body.get("turn"), "log": reply.log}


class _OverHttp:
    """Runs the same turns against a deployed service, which also proves the deploy."""

    def __init__(self, base_url: str, *, timeout: int) -> None:
        self._url = base_url.rstrip("/") + RESPOND_PATH
        self._timeout = timeout

    @property
    def target(self) -> str:
        return self._url

    def ask(self, session_id: str, message: str) -> dict[str, Any]:
        request = urllib.request.Request(
            self._url,
            data=json.dumps({"session_id": session_id, "message": message}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as response:
                body = json.loads(response.read().decode("utf-8"))
            return {"ok": True, "status": response.status, "reply": body.get("reply", ""),
                    "turn": body.get("turn"), "log": None}
        except urllib.error.HTTPError as error:
            try:
                body = json.loads(error.read().decode("utf-8"))
                detail = body.get("error", {})
            except Exception:  # noqa: BLE001 - an error page is not always JSON
                detail = {}
            return {"ok": False, "status": error.code, "code": detail.get("code", "http_error"),
                    "message": detail.get("message", error.reason)}
        except Exception as error:  # noqa: BLE001 - a deployment that is not answering
            return {"ok": False, "status": 0, "code": "unreachable", "message": str(error)}


def _ask(
    runner: Any,
    session_id: str,
    prompt: dict[str, Any],
    *,
    clock: Any,
    expected_turn: int | None = None,
) -> dict[str, Any]:
    """One turn, asked and graded, in the shape the report carries it."""
    started = clock()
    outcome = runner.ask(session_id, prompt["message"])
    entry: dict[str, Any] = {
        "id": prompt["id"],
        "message": prompt["message"],
        "expects": prompt["expects"],
        "latency_ms": int((clock() - started) * 1000),
        "ok": outcome["ok"],
        "http_status": outcome["status"],
    }
    if expected_turn is not None:
        entry["expected_turn"] = expected_turn
        entry["reported_turn"] = outcome.get("turn")
    if outcome["ok"]:
        entry["reply"] = outcome["reply"]
        entry["checks"] = _grade(
            prompt,
            outcome["reply"],
            expected_turn=expected_turn,
            reported_turn=outcome.get("turn"),
        )
        log = outcome.get("log") or {}
        entry["rounds"] = log.get("rounds")
        entry["acts"] = log.get("acts")
        entry["refused"] = log.get("refused")
    else:
        entry["error"] = {"code": outcome["code"], "message": outcome["message"]}
        entry["checks"] = [
            _check("answered", False, f"{outcome['code']}: {outcome['message']}", kind="hard")
        ]
    return entry


def run(runner: Any, *, now: Any = None) -> dict[str, Any]:
    """The committed turns -- three alone, then three in one conversation -- reported once."""
    clock = now or (lambda: time.monotonic())
    turns = [
        _ask(runner, f"acceptance-{uuid.uuid4().hex}", prompt, clock=clock)
        for prompt in fixture.acceptance_prompts()
    ]

    # One session id for all three, which is the whole point: the second and third turns are
    # unanswerable without the first. Asked in order, and a turn that failed still leaves the
    # ones after it asked -- the report is more useful saying which turn lost the thread than
    # stopping at it.
    conversation = fixture.acceptance_conversation()
    session_id = f"acceptance-conversation-{uuid.uuid4().hex}"
    conversation_turns = [
        _ask(runner, session_id, prompt, clock=clock, expected_turn=index)
        for index, prompt in enumerate(conversation["turns"], start=1)
    ]

    graded = [*turns, *conversation_turns]
    hard_failures = [
        f"{turn['id']}/{check['check']}"
        for turn in graded
        for check in turn["checks"]
        if check["kind"] == "hard" and not check["passed"]
    ]
    hints = [
        f"{turn['id']}/{check['check']}"
        for turn in graded
        for check in turn["checks"]
        if check["kind"] == "hint" and not check["passed"]
    ]
    # A tool round having run at all, anywhere, is what shows the continuation works. The
    # second turn needs evidence the first read leaves out, so on a healthy run it is this
    # one that proves it.
    tool_rounds = [turn["id"] for turn in graded if turn.get("acts")]
    return {
        "target": runner.target,
        "model": model_module.MODEL,
        "ran_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "turns": turns,
        "conversation": {
            "id": conversation["id"],
            "why": conversation["why"],
            "session_id": session_id,
            "turns": conversation_turns,
        },
        "passed": not hard_failures,
        "hard_failures": hard_failures,
        "hint_failures": hints,
        "turns_using_a_tool_round": tool_rounds,
    }


def _render_turn(turn: dict[str, Any], lines: list[str]) -> None:
    head = "PASS" if all(
        check["passed"] for check in turn["checks"] if check["kind"] == "hard"
    ) else "FAIL"
    lines.append(f"[{head}] {turn['id']}  {turn['latency_ms']} ms")
    if turn.get("rounds") is not None:
        acts = ", ".join(turn.get("acts") or []) or "none"
        lines.append(f"       rounds: {turn['rounds']}   acts: {acts}")
    if not turn["ok"]:
        lines.append(f"       error: {turn['error']['code']} -- {turn['error']['message']}")
    for check in turn["checks"]:
        mark = "ok " if check["passed"] else ("!! " if check["kind"] == "hard" else "?? ")
        lines.append(f"       {mark}{check['check']}: {check['detail']}")
    lines.append("")


def render(report: dict[str, Any]) -> str:
    """The report an operator reads in a terminal."""
    lines = [
        f"Long Run Hybrid Coach demo acceptance -- {report['model']}",
        f"target: {report['target']}",
        f"ran at: {report['ran_at']}",
        "",
    ]
    for turn in report["turns"]:
        _render_turn(turn, lines)
    conversation = report.get("conversation")
    if conversation:
        lines.append(f"-- {conversation['id']}: three turns, one session --")
        lines.append("")
        for turn in conversation["turns"]:
            _render_turn(turn, lines)
    lines.append(
        "tool rounds ran on: "
        + (", ".join(report["turns_using_a_tool_round"]) or "none -- the continuation was not exercised")
    )
    if report["hint_failures"]:
        lines.append(
            "hints to read the transcript for: " + ", ".join(report["hint_failures"])
        )
    lines.append("PASSED" if report["passed"] else "FAILED: " + ", ".join(report["hard_failures"]))
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python3 -m entrypoints.demo.acceptance",
        description=(
            "Run the committed demo acceptance turns against the real model: three on their "
            "own, then three in one conversation."
        ),
    )
    parser.add_argument(
        "--base-url",
        help=(
            "Run against a deployed service instead of in this process. "
            f"Production is {PUBLIC_ENDPOINT.removesuffix(RESPOND_PATH)}"
        ),
    )
    parser.add_argument("--out", help="Where to write the JSON report (default: a temp file)")
    parser.add_argument("--timeout", type=int, default=120, help="Per-turn timeout in seconds")
    args = parser.parse_args(argv)

    if args.base_url:
        runner: Any = _OverHttp(args.base_url, timeout=args.timeout)
    else:
        config = from_environment()
        if not config.has_api_key:
            print(
                "OPENAI_API_KEY is not set. This command makes real model calls; set the "
                "credential, or pass --base-url to run against a deployment that has one.",
                file=sys.stderr,
            )
            return 2
        runner = _InProcess(DemoService(config))

    report = run(runner)
    destination = Path(args.out) if args.out else Path(
        tempfile.gettempdir(), f"demo-acceptance-{int(time.time())}.json"
    )
    destination.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(render(report))
    print(f"\nfull transcripts: {destination}")
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
