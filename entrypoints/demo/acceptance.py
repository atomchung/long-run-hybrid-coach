"""The one command that proves this demo works against the real model.

Everything else in this directory is held by tests that never reach a network. What no
test here can answer is whether `gpt-5.6-luna` actually produces a usable answer from this
fixture and this prompt, and whether the request shape this repository built is the one the
Responses API accepts. That question needs a credential and a real call, so it lives in a
command an operator runs deliberately:

    OPENAI_API_KEY=... python3 -m entrypoints.demo.acceptance

That runs the three committed acceptance turns in three separate conversations, in this
process, against the live API. `--base-url https://demo-api.paceandstaystrong.com` runs the
same three turns over HTTP against a deployed service instead, which is the version that
also proves CORS, the platform port and the deploy.

## What it reports, and what it does not

Per turn: whether it completed, how long it took, how many model rounds it used, which
demo acts ran, and a short list of **checks**. Two of those checks are real and two are
hints, and the report says which is which:

*Real*: the turn returned an answer at all; no error code came back; and the reply does not
claim to have saved, delivered or scheduled anything -- a demo that says it updated a
calendar is wrong in a way that matters, and a substring is enough to catch it. So is a
reply naming a score this product does not have.

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
}


def _check(name: str, passed: bool, detail: str, *, kind: str) -> dict[str, Any]:
    return {"check": name, "kind": kind, "passed": passed, "detail": detail}


def _grade(prompt: dict[str, Any], reply: str) -> list[dict[str, Any]]:
    lowered = reply.lower()
    claimed = [phrase for phrase in CLAIMED_WRITE if phrase in lowered]
    invented = [phrase for phrase in INVENTED_SCORE if phrase in lowered]
    missing = [word for word in HINTS.get(prompt["id"], ()) if word not in lowered]
    return [
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
            "reads_like_the_expected_answer",
            not missing,
            "expected wording present" if not missing else f"absent: {', '.join(missing)}",
            kind="hint",
        ),
    ]


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
                "log": reply.log}


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
                    "log": None}
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


def run(runner: Any, *, now: Any = None) -> dict[str, Any]:
    """The three committed turns, each in its own conversation, with one report back."""
    clock = now or (lambda: time.monotonic())
    turns: list[dict[str, Any]] = []
    for prompt in fixture.acceptance_prompts():
        session_id = f"acceptance-{uuid.uuid4().hex}"
        started = clock()
        outcome = runner.ask(session_id, prompt["message"])
        elapsed_ms = int((clock() - started) * 1000)
        entry: dict[str, Any] = {
            "id": prompt["id"],
            "message": prompt["message"],
            "expects": prompt["expects"],
            "latency_ms": elapsed_ms,
            "ok": outcome["ok"],
            "http_status": outcome["status"],
        }
        if outcome["ok"]:
            entry["reply"] = outcome["reply"]
            entry["checks"] = _grade(prompt, outcome["reply"])
            log = outcome.get("log") or {}
            entry["rounds"] = log.get("rounds")
            entry["acts"] = log.get("acts")
            entry["refused"] = log.get("refused")
        else:
            entry["error"] = {"code": outcome["code"], "message": outcome["message"]}
            entry["checks"] = [
                _check("answered", False, f"{outcome['code']}: {outcome['message']}", kind="hard")
            ]
        turns.append(entry)

    hard_failures = [
        f"{turn['id']}/{check['check']}"
        for turn in turns
        for check in turn["checks"]
        if check["kind"] == "hard" and not check["passed"]
    ]
    hints = [
        f"{turn['id']}/{check['check']}"
        for turn in turns
        for check in turn["checks"]
        if check["kind"] == "hint" and not check["passed"]
    ]
    # A tool round having run at all, anywhere, is what shows the continuation works. The
    # second turn needs evidence the first read leaves out, so on a healthy run it is this
    # one that proves it.
    tool_rounds = [turn["id"] for turn in turns if turn.get("acts")]
    return {
        "target": runner.target,
        "model": model_module.MODEL,
        "ran_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "turns": turns,
        "passed": not hard_failures,
        "hard_failures": hard_failures,
        "hint_failures": hints,
        "turns_using_a_tool_round": tool_rounds,
    }


def render(report: dict[str, Any]) -> str:
    """The report an operator reads in a terminal."""
    lines = [
        f"Long Run Hybrid Coach demo acceptance -- {report['model']}",
        f"target: {report['target']}",
        f"ran at: {report['ran_at']}",
        "",
    ]
    for turn in report["turns"]:
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
        description="Run the three committed demo acceptance turns against the real model.",
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
