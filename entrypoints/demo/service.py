"""One demo turn, end to end: limits, the model call, the boundary, and the reply.

The transport is in ``server``; everything decided about a request is decided here, so the
whole of it can be exercised without a socket.

What the model is given is assembled from three things, and only one of them is written
here. The training judgment is ``garmin_coach_loop``'s own served prompt, reused verbatim
-- it is where this product's coaching lives, and a second copy of it in this directory
would be a second coach that drifts (AGENTS.md 11). The athlete is the committed fixture,
projected through the gateway's own evidence reader. The third is ``orchestration.md``
beside this file, which is short and exists because the product's own orchestration prompt
describes a tool surface the demo does not have: it names prepare, confirm and apply, and
pointing a model at that here would be telling it to reach for doors that are not in the
building.

Nothing about a request can raise a limit, widen the boundary, choose a model, or reach
another session. A request supplies exactly two things: an opaque session id and a sentence.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import threading
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from garmin_coach_loop import orchestration

from . import boundary, fixture, model as model_module
from .config import DemoConfig
from .sessions import SessionLimit, SessionStore, session_fingerprint, valid_session_id


LOGGER = logging.getLogger("entrypoints.demo")

RESPOND_PATH = "/demo/v1/respond"
HEALTH_PATH = "/healthz"

_ORCHESTRATION = Path(__file__).with_name("orchestration.md")

# How many times one turn may go round the model-then-act loop before it has to answer in
# words. A ceiling rather than a budget: a turn that has asked for evidence twice has the
# fixture, and the cost of a loop that never ends is paid by an anonymous caller's request.
MAX_TOOL_ROUNDS = 3

# Per session, across all its turns. The fixture is about 23 KB whole, so this is roughly
# "the athlete, several times over" and is here to bound one visitor's share of memory
# rather than to shape an answer.
MAX_HISTORY_CHARS = 96 * 1024


class DemoRequestError(Exception):
    """A request answered with a status and a machine-readable code."""

    def __init__(self, status: int, code: str, message: str, *, retry_after: int | None = None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.retry_after = retry_after


@dataclass(frozen=True)
class DemoReply:
    """What the transport sends back, and what the access log is allowed to say about it."""

    status: int
    body: dict[str, Any]
    log: dict[str, Any]
    retry_after: int | None = None


class _FixedWindowLimiter:
    """Requests per minute, per key, counted in whole minutes.

    A fixed window rather than a token bucket: it is a handful of integers, it cannot leak
    memory across a long run because the window key changes, and the failure it exists to
    prevent -- one client discovering the endpoint and holding it open -- is not sensitive
    to the boundary effect a fixed window has.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._window: int | None = None
        self._counts: dict[str, int] = defaultdict(int)

    def check(self, key: str, *, limit: int, now: dt.datetime) -> int | None:
        """``None`` when the request may proceed, else the seconds until the window turns."""
        window = int(now.timestamp()) // 60
        with self._lock:
            if window != self._window:
                self._window = window
                self._counts = defaultdict(int)
            if self._counts[key] >= limit:
                return 60 - int(now.timestamp()) % 60
            self._counts[key] += 1
            return None


class DemoService:
    """Everything one deployment of the demo knows how to do."""

    def __init__(
        self,
        config: DemoConfig,
        *,
        client: Any | None = None,
        now: Any | None = None,
    ) -> None:
        self.config = config
        self._client = client or model_module.ResponsesClient(
            api_key=config.api_key,
            base_url=config.model_base_url,
            timeout_seconds=config.model_timeout_seconds,
        )
        self._now = now or (lambda: dt.datetime.now(dt.timezone.utc))
        self._sessions = SessionStore(
            ttl_seconds=config.session_ttl_seconds,
            max_sessions=config.max_sessions,
            max_turns=config.max_turns_per_session,
        )
        self._client_limiter = _FixedWindowLimiter()
        self._global_limiter = _FixedWindowLimiter()
        self._fixture_report = fixture.validate()
        self._instructions: str | None = None

    # ---------------------------------------------------------------- what the model reads
    def instructions(self) -> str:
        """The demo's orchestration, the product's training judgment, and the coach read.

        Assembled once. Every session reads the same athlete, so this text is the same for
        all of them and the same on every round of one turn -- what differs between two
        conversations is what was said in them, and that is the history rather than this.
        """
        if self._instructions is None:
            self._instructions = self._compose_instructions()
        return self._instructions

    def _compose_instructions(self) -> str:
        plan = fixture.plan_state()
        view, index = fixture.projected(None)
        read = {
            "plan_state": {
                "present": True,
                "plan_id": plan["plan_id"],
                "plan_version": plan["version"],
                "current_plan": plan,
            },
            "context": view,
        }
        if index is not None:
            read["evidence_index"] = index
        return "\n\n".join(
            [
                _ORCHESTRATION.read_text(encoding="utf-8").rstrip("\r\n"),
                orchestration.training_judgment(),
                "# This athlete's current read\n\n"
                "```json\n" + json.dumps(read, indent=1, sort_keys=True) + "\n```",
            ]
        )

    # -------------------------------------------------------------------------- the health
    def health(self) -> DemoReply:
        """Whether this process can answer, without saying anything about who has asked.

        The fixture report is part of it: a fixture that no longer validates against the
        contracts is a demo answering from a shape the product does not have, and that is a
        deployment problem rather than a request one.
        """
        degraded = bool(self._fixture_report["errors"]) or not self.config.has_api_key
        body = {
            "status": "degraded" if degraded else "ok",
            "model": model_module.MODEL,
            "model_credential": "present" if self.config.has_api_key else "absent",
            "fixture": "invalid" if self._fixture_report["errors"] else "valid",
            "sessions": len(self._sessions),
        }
        return DemoReply(
            status=503 if degraded else 200,
            body=body,
            log={"event": "health", "status": body["status"]},
        )

    # ------------------------------------------------------------------------- the one turn
    def respond(self, payload: Any, *, client_key: str) -> DemoReply:
        """Answer one ``POST /demo/v1/respond``.

        Raises ``DemoRequestError`` for everything a caller can get wrong; the transport
        turns that into the status and the body. Nothing below writes anything outside this
        process, and the only mutable state it touches is the named session's own.
        """
        now = self._now()
        started = now

        # Per-client first, so one caller who will not stop spends their own allowance
        # rather than the ceiling everybody else is sharing.
        retry_after = self._client_limiter.check(
            client_key, limit=self.config.requests_per_minute_per_client, now=now
        )
        if retry_after is None:
            retry_after = self._global_limiter.check(
                "all", limit=self.config.requests_per_minute_global, now=now
            )
        if retry_after is not None:
            raise DemoRequestError(
                429, "rate_limited", "too many requests; try again shortly",
                retry_after=retry_after,
            )

        if not isinstance(payload, dict):
            raise DemoRequestError(400, "invalid_request", "the body must be a JSON object")
        unexpected = sorted(set(payload) - {"session_id", "message"})
        if unexpected:
            raise DemoRequestError(
                400, "invalid_request", f"unexpected field: {', '.join(unexpected)}"
            )
        session_id = payload.get("session_id")
        if not valid_session_id(session_id):
            raise DemoRequestError(
                400,
                "session_id_invalid",
                "session_id must be 8-128 characters of A-Z, a-z, 0-9, '-' or '_'",
            )
        message = payload.get("message")
        if not isinstance(message, str) or not message.strip():
            raise DemoRequestError(400, "invalid_request", "message must be a non-empty string")
        if len(message) > self.config.max_message_chars:
            raise DemoRequestError(
                400,
                "message_too_long",
                f"message must be at most {self.config.max_message_chars} characters",
            )
        if not self.config.has_api_key:
            # Said in the same words whether the key was never set or was rejected. A demo
            # that answered without one would have to answer from something else, and there
            # is deliberately nothing else for it to answer from.
            raise DemoRequestError(
                503,
                "demo_model_unconfigured",
                "the demo is not configured with a model credential and cannot answer",
            )

        session = self._sessions.get_or_create(session_id, now=now)
        try:
            turn_index = self._sessions.begin_turn(session)
        except SessionLimit as limit:
            raise DemoRequestError(409, "turn_limit_reached", str(limit)) from None

        items = list(session.history)
        items.append({"role": "user", "content": message.strip()})

        acts: list[str] = []
        refused: list[str] = []
        text = ""
        for _ in range(MAX_TOOL_ROUNDS):
            try:
                turn = self._client.respond(
                    instructions=self.instructions(),
                    input_items=items,
                    tools=boundary.tool_definitions(),
                )
            except model_module.ModelError as error:
                status = {
                    "demo_model_unconfigured": 503,
                    "model_rate_limited": 429,
                    "model_timeout": 504,
                }.get(error.code, 502)
                raise DemoRequestError(status, error.code, str(error)) from None
            text = turn.text
            if not turn.tool_calls:
                break
            items.extend(dict(item) for item in turn.output_items)
            for call in turn.tool_calls:
                result = boundary.dispatch(
                    call.name,
                    call.arguments,
                    before=session.plan_state,
                    issued_at=now,
                )
                acts.append(call.name)
                if result.get("code") == "demo_write_forbidden":
                    refused.append(call.name)
                if result.get("status") == "preview_only":
                    # Exploratory, and this session's alone: the preview is kept so the
                    # conversation can refer back to it, and the plan it was projected
                    # against is untouched by construction.
                    session.previews.append(result["preview"])
                    del session.previews[:-3]
                items.append(
                    {
                        "type": "function_call_output",
                        "call_id": call.call_id,
                        "output": json.dumps(result, sort_keys=True),
                    }
                )
        else:
            # Out of rounds with the model still asking. Whatever words it produced stand,
            # and the fallback below covers the case where it produced none.
            LOGGER.info(
                json.dumps({"event": "tool_rounds_exhausted", "session": session.fingerprint})
            )

        if not text.strip():
            text = (
                "I could not put that into an answer this time. Ask me again, or ask "
                "something narrower about this athlete's week."
            )

        items.append({"role": "assistant", "content": text})
        session.history = _trimmed(items)

        finished = self._now()
        return DemoReply(
            status=200,
            body={"reply": text},
            log={
                "event": "respond",
                "session": session.fingerprint,
                "turn": turn_index,
                "acts": acts,
                "refused": refused,
                "reply_chars": len(text),
                "duration_ms": int((finished - started).total_seconds() * 1000),
            },
        )

    # ------------------------------------------------------------------------ housekeeping
    def purge_expired(self) -> int:
        return self._sessions.purge_expired(now=self._now())

    @property
    def sessions(self) -> SessionStore:
        return self._sessions

    @property
    def fixture_report(self) -> dict[str, list[str]]:
        return self._fixture_report


def _trimmed(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Drop the oldest exchanges once one conversation has grown past its share.

    Oldest first, and never the most recent items: a conversation that loses its last turn
    stops being a conversation. The evidence a dropped tool result carried is still one
    ``readDemoEvidence`` away, so nothing becomes unrecoverable by being trimmed.
    """
    while len(items) > 4 and len(json.dumps(items)) > MAX_HISTORY_CHARS:
        del items[0]
    return items


def error_body(error: DemoRequestError) -> dict[str, Any]:
    return {"error": {"code": error.code, "message": error.message}}
