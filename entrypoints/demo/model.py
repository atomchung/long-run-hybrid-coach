"""The one outbound call this service makes, and every provider-specific shape it needs.

Stdlib only -- ``urllib.request`` rather than a vendor SDK -- so the demo image installs
nothing. The gateway image has exactly one dependency, the MCP SDK (AGENTS.md,
"Dependencies"); this service has none, and it must acquire neither that one nor a
vendor's client.

Everything that knows what the Responses API looks like is in this file: how a request is
built, which output items have to be carried into the next round, how a tool result is
spelled, and how a failure maps onto this service's own error codes. ``service.py`` drives
a conversation and never names a provider field; ``garmin_coach_loop`` does not know this
file exists.

Four things are deliberate and none is configurable from a request:

*The model is pinned.* ``MODEL`` is a constant with no environment override and no fallback
to a smaller or older model when a call fails -- a demo that quietly answers from something
else is a demo whose answers mean nothing. A failure is reported as a failure.

*Nothing is stored at the provider.* ``store`` is false on every call. That makes the
conversation stateless, which is what the reasoning carry-forward below exists for.

*The key comes from the server's environment and goes nowhere else.* It is read once into
the configuration, sent only in this request's ``Authorization`` header, and never logged,
never echoed into an error, and never included in any response this service writes.

*Reasoning effort is medium.* Measured against the deployed service on 2026-09-17: at
``max`` the first round alone asks for three evidence reads, and every result is carried
into the next round's input on top of 42 KB of instructions, so the second and third rounds
run past the 60-second provider timeout and the visitor gets a 504 instead of an answer.
``medium`` still uses a tool round -- the demo's whole point is that it reads the evidence
before answering -- where ``low`` answered straight from the instructions without reading
anything.
"""

from __future__ import annotations

import http.client
import json
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable, Iterator


# Pinned. See the module note -- this is not a default, and there is no other value.
MODEL = "gpt-5.6-luna"

# One of none | minimal | low | medium | high | xhigh | max.
REASONING_EFFORT = "medium"

# An upper bound on *everything* the model generates for one response, reasoning tokens
# included -- which is why this is not the size of the answer. A three-option allocation
# with its evidence runs to a few hundred visible tokens; the rest is headroom so a turn
# that thinks a little longer comes back as an answer rather than as a truncation. It was
# 2000, which the reasoning budget alone can spend.
MAX_OUTPUT_TOKENS = 8000

# Carried in ``include``. With ``store: false`` the provider returns encrypted reasoning by
# default and this value is accepted for compatibility rather than required, so asking for
# it explicitly costs nothing and states the dependency in the request itself.
ENCRYPTED_REASONING = "reasoning.encrypted_content"


# The provider's own words for a 429 that no amount of waiting clears: the account behind
# the credential has no credit left. Matched against both the error body's ``type`` and its
# ``code``, because the refusal has been seen naming itself in either field.
_QUOTA_REFUSALS = frozenset({"insufficient_quota", "credit_balance_exhausted"})

# A hard stop on how much of an error body is buffered to classify one failure. Far more
# than any refusal needs -- the words above arrive in a few hundred bytes -- and generous
# enough that a wordy message does not truncate the JSON and take the classification with
# it, which would quietly turn "out of credit" back into "try again shortly".
_ERROR_BODY_LIMIT = 16384

# A completed event includes the entire output, including encrypted reasoning. Bound each
# line/event and the whole round even when the upstream is malformed or never terminates.
_STREAM_EVENT_LIMIT = 2 * 1024 * 1024
_STREAM_TOTAL_LIMIT = 8 * 1024 * 1024


class ModelError(Exception):
    """A call that did not produce an answer, with the code the client is told.

    ``provider_type`` is the provider's one-word name for the refusal, and it exists for
    the log alone. Two failures this service has to describe to a visitor in the same
    sentence are still different jobs for whoever is on call. It never reaches a response
    -- see ``_http_error``.
    """

    def __init__(self, code: str, message: str, *, provider_type: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.provider_type = provider_type


class MissingApiKey(ModelError):
    def __init__(self) -> None:
        super().__init__(
            "demo_model_unconfigured",
            "the demo is not configured with a model credential and cannot answer",
        )


@dataclass(frozen=True)
class ToolCall:
    """One act the model asked for, as the Responses API reports it."""

    call_id: str
    name: str
    arguments: Any
    raw: dict[str, Any]


@dataclass(frozen=True)
class Usage:
    """The provider's token counts, for one call or a sum of calls.

    A count missing from any call leaves that sum unknown. These numbers measure token
    usage; they do not measure a price or explain how much time reasoning took.
    """

    input_tokens: int | None = None
    cached_tokens: int | None = None
    output_tokens: int | None = None
    reasoning_tokens: int | None = None

    def __add__(self, other: "Usage") -> "Usage":
        def plus(left: int | None, right: int | None) -> int | None:
            if left is None or right is None:
                return None
            return left + right

        return Usage(
            plus(self.input_tokens, other.input_tokens),
            plus(self.cached_tokens, other.cached_tokens),
            plus(self.output_tokens, other.output_tokens),
            plus(self.reasoning_tokens, other.reasoning_tokens),
        )

    def as_log(self) -> dict[str, int]:
        """Only complete counts, so a subtotal cannot masquerade as a turn total."""
        return {
            name: value
            for name, value in (
                ("input_tokens", self.input_tokens),
                ("cached_tokens", self.cached_tokens),
                ("output_tokens", self.output_tokens),
                ("reasoning_tokens", self.reasoning_tokens),
            )
            if value is not None
        }


def _usage(payload: dict[str, Any]) -> Usage:
    """The provider's ``usage`` block, read for four numbers and nothing else."""
    raw = payload.get("usage")
    if not isinstance(raw, dict):
        return Usage()

    def number(source: Any, key: str) -> int | None:
        value = source.get(key) if isinstance(source, dict) else None
        return value if type(value) is int and value >= 0 else None

    return Usage(
        input_tokens=number(raw, "input_tokens"),
        cached_tokens=number(raw.get("input_tokens_details"), "cached_tokens"),
        output_tokens=number(raw, "output_tokens"),
        reasoning_tokens=number(raw.get("output_tokens_details"), "reasoning_tokens"),
    )


@dataclass(frozen=True)
class ModelTurn:
    """One response: the words, the acts it asked for, and the items it produced."""

    text: str
    tool_calls: tuple[ToolCall, ...] = ()
    output_items: tuple[dict[str, Any], ...] = field(default=())
    usage: Usage = field(default_factory=Usage)

    @property
    def reasoning_items(self) -> tuple[dict[str, Any], ...]:
        return tuple(item for item in self.output_items if item.get("type") == "reasoning")


def build_request(
    *,
    instructions: str,
    input_items: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None = None,
    allow_tools: bool = True,
) -> dict[str, Any]:
    """The request body, assembled in one place so a test can read it without a network.

    ``allow_tools`` false still sends the tools -- the conversation being carried contains
    ``function_call`` items, and input that refers to tools the request does not declare is
    input the API can refuse -- but forbids another call. It is how a turn is made to answer
    in words on its last round instead of asking for a fourth thing it will not get.
    """
    body: dict[str, Any] = {
        "model": MODEL,
        "instructions": instructions,
        "input": input_items,
        "max_output_tokens": MAX_OUTPUT_TOKENS,
        "reasoning": {"effort": REASONING_EFFORT},
        "include": [ENCRYPTED_REASONING],
        "store": False,
    }
    if tools:
        body["tools"] = tools
        body["tool_choice"] = "auto" if allow_tools else "none"
    return body


def carry_forward(output_items: tuple[dict[str, Any], ...]) -> list[dict[str, Any]]:
    """Everything one response produced, echoed into the next request's input, in order.

    This is what makes a multi-round tool conversation work at all while ``store`` is
    false. The provider is keeping nothing, so the next round's input *is* the whole
    conversation -- and the items that matter most are the ones easiest to drop by
    accident: a ``reasoning`` item carries the model's own chain for this turn as
    ``encrypted_content``, and leaving it out makes every round after a tool call start
    over from the words alone.

    Verbatim, and in the order the response gave them, rather than filtered to the types
    this file recognises. A reasoning item belongs immediately before the call it reasoned
    towards, a call must be followed by its output, and the cheapest way to keep both true
    is to not rearrange anything. An item type added to the API later rides along instead
    of being silently discarded here.
    """
    return [dict(item) for item in output_items]


def function_call_output(call_id: str, result: Any) -> dict[str, Any]:
    """One tool result, in the shape the next request takes it in."""
    return {
        "type": "function_call_output",
        "call_id": call_id,
        "output": json.dumps(result, sort_keys=True),
    }


def assistant_message(text: str) -> dict[str, Any]:
    """A sentence this service supplied, kept in the conversation as the model's turn."""
    return {"role": "assistant", "content": text}


def _collect_text(payload: dict[str, Any]) -> str:
    """The reply, from whichever shape the response states it in.

    ``output_text`` is a convenience some responses carry; ``output`` is the one that is
    always there. Reading both, in that order, means a response that carries only the
    canonical shape still answers.
    """
    direct = payload.get("output_text")
    if isinstance(direct, str) and direct.strip():
        return direct.strip()
    chunks: list[str] = []
    for item in payload.get("output") or []:
        if not isinstance(item, dict) or item.get("type") != "message":
            continue
        for part in item.get("content") or []:
            if isinstance(part, dict) and part.get("type") == "output_text":
                text = part.get("text")
                if isinstance(text, str):
                    chunks.append(text)
    return "".join(chunks).strip()


def _collect_tool_calls(payload: dict[str, Any]) -> tuple[ToolCall, ...]:
    calls: list[ToolCall] = []
    for item in payload.get("output") or []:
        if not isinstance(item, dict) or item.get("type") != "function_call":
            continue
        raw_arguments = item.get("arguments")
        try:
            arguments = (
                json.loads(raw_arguments) if isinstance(raw_arguments, str) else raw_arguments
            )
        except json.JSONDecodeError:
            # Handed on as it came. The boundary refuses arguments it cannot read, and it
            # is a better place to say so than a parser that has no idea what was asked.
            arguments = None
        calls.append(
            ToolCall(
                call_id=str(item.get("call_id") or item.get("id") or ""),
                name=str(item.get("name") or ""),
                arguments=arguments,
                raw=item,
            )
        )
    return tuple(calls)


def parse_response(payload: dict[str, Any]) -> ModelTurn:
    """One response body, read into the only three things this service needs from it."""
    text = _collect_text(payload)
    tool_calls = _collect_tool_calls(payload)
    output_items = tuple(
        item for item in (payload.get("output") or []) if isinstance(item, dict)
    )
    if not text and not tool_calls:
        reason = (payload.get("incomplete_details") or {}).get("reason")
        if payload.get("status") == "incomplete" and reason == "max_output_tokens":
            # The whole budget went on reasoning. A distinct code because the fix is a
            # deployment one -- raise MAX_OUTPUT_TOKENS -- rather than "try again".
            raise ModelError(
                "model_output_truncated",
                "the model reached its output ceiling before it answered",
            )
        raise ModelError("model_unavailable", "the model returned no answer")
    return ModelTurn(
        text=text,
        tool_calls=tool_calls,
        output_items=output_items,
        usage=_usage(payload),
    )


def _refusal(error: urllib.error.HTTPError) -> tuple[str | None, bool]:
    """The provider's word for a failure, and whether it is the kind that never clears.

    The body is opened to answer those two questions and for nothing else. What comes back
    is one short word and one boolean; the message, and everything else that could quote
    an anonymous visitor's request back at them, is dropped here rather than carried and
    filtered later.

    A body that will not read -- absent, truncated, not JSON, a shape this does not know --
    is not a failure of its own. It leaves the call classified by its status alone, which
    is what this file did before it read bodies at all.
    """
    try:
        raw = error.read(_ERROR_BODY_LIMIT)
        payload = json.loads(raw.decode("utf-8", "replace"))
    except Exception:  # noqa: BLE001 - a body this cannot read is simply not read
        return None, False
    if not isinstance(payload, dict):
        return None, False
    # Both spellings: the provider nests the failure under ``error``, and the same object
    # is quoted flat in the reports that reach this repository.
    body = payload.get("error") if isinstance(payload.get("error"), dict) else payload
    kind = body.get("type")
    words = {word for word in (kind, body.get("code")) if isinstance(word, str)}
    # Bounded the way ``server._client_key`` bounds an address: this is the one string the
    # provider wrote that reaches a log line, and a log line is not the place to discover
    # how long a provider can make one word.
    return (kind[:64] if isinstance(kind, str) else None), bool(words & _QUOTA_REFUSALS)


def _stream_error() -> ModelError:
    return ModelError("model_unavailable", "the model returned an unreadable stream")


def _sse_events(response: Any) -> Iterator[tuple[str, dict[str, Any]]]:
    """Read bounded UTF-8 SSE events, never forwarding the provider's event wholesale."""
    data: list[str] = []
    event = ""
    event_size = total = 0
    while True:
        raw = response.readline(_STREAM_EVENT_LIMIT + 1)
        if not raw:
            if data:
                raise _stream_error()
            return
        total += len(raw)
        event_size += len(raw)
        if event_size > _STREAM_EVENT_LIMIT or total > _STREAM_TOTAL_LIMIT:
            raise _stream_error()
        try:
            line = raw.decode("utf-8").rstrip("\r\n")
        except UnicodeDecodeError:
            raise _stream_error() from None
        if not line:
            if data:
                try:
                    payload = json.loads("\n".join(data))
                except json.JSONDecodeError:
                    raise _stream_error() from None
                if not isinstance(payload, dict):
                    raise _stream_error()
                yield event, payload
            data, event, event_size = [], "", 0
        elif line.startswith("data:"):
            data.append(line[5:].removeprefix(" "))
        elif line.startswith("event:"):
            event = line[6:].strip()


def _provider_stream_error(payload: dict[str, Any]) -> ModelError:
    """Stable local errors only; provider messages may echo the visitor's input."""
    body = payload.get("error")
    body = body if isinstance(body, dict) else payload
    code = body.get("code")
    code = code if isinstance(code, str) else None
    kind = body.get("type")
    words = {word for word in (code, kind) if isinstance(word, str)}
    if words & _QUOTA_REFUSALS:
        return ModelError("model_quota_exhausted", "this demo is temporarily unavailable")
    if code == "rate_limit_exceeded":
        return ModelError("model_rate_limited", "the demo is busy; try again shortly")
    if code in ("invalid_api_key", "authentication_error"):
        return ModelError("demo_model_unconfigured", "the demo's model credential was not accepted")
    if code in ("timeout", "request_timeout"):
        return ModelError("model_timeout", "the model did not answer in time")
    return ModelError("model_unavailable", "the model could not answer this turn")


def _read_stream(
    response: Any, *, on_text: Callable[[str], None], allow_tools: bool
) -> ModelTurn:
    # Only final_answer messages can be displayed while tools remain possible. A message
    # with no phase can be a preamble to a tool call, so it waits for the completed body.
    # The forced no-tools last round can stream unphased messages too. Commentary never
    # goes to a visitor, but stays verbatim in output_items for the next provider call.
    messages: dict[int, dict[str, Any]] = {}
    emitted = ""
    saw_tool = False
    for event, payload in _sse_events(response):
        kind = payload.get("type")
        if not isinstance(kind, str) or (event and event != kind):
            raise _stream_error()
        if kind == "response.output_item.added":
            index, item = payload.get("output_index"), payload.get("item")
            if (type(index) is not int or index < 0 or not isinstance(item, dict)
                    or index in messages):
                raise _stream_error()
            if item.get("type") == "message" and (
                item.get("role") != "assistant" or not isinstance(item.get("id"), str)
                or not item["id"]
            ):
                raise _stream_error()
            messages[index] = item
            saw_tool = saw_tool or item.get("type") == "function_call"
            if saw_tool and emitted:
                raise _stream_error()
        elif kind == "response.output_text.delta":
            index = payload.get("output_index")
            if type(index) is not int:
                raise _stream_error()
            item = messages.get(index)
            delta = payload.get("delta")
            if not item or item.get("type") != "message" or not isinstance(delta, str):
                raise _stream_error()
            if payload.get("item_id") != item.get("id"):
                raise _stream_error()
            phase = item.get("phase")
            visible = phase == "final_answer" or (phase is None and not allow_tools)
            if visible and not saw_tool and delta:
                emitted += delta
                on_text(delta)
        elif kind == "response.completed":
            completed = payload.get("response")
            if not isinstance(completed, dict) or completed.get("status") != "completed":
                raise _stream_error()
            output = completed.get("output")
            if not isinstance(output, list) or not all(isinstance(item, dict) for item in output):
                raise _stream_error()
            for item in output:
                if item.get("type") == "message":
                    content = item.get("content")
                    if (item.get("role") != "assistant" or not isinstance(content, list)
                            or not all(isinstance(part, dict) for part in content)
                            or any(part.get("type") == "output_text" and not isinstance(part.get("text"), str)
                                   for part in content)):
                        raise _stream_error()
            # Do not use output_text's convenience aggregate: it may include commentary.
            has_final = any(item.get("type") == "message" and item.get("phase") == "final_answer"
                            for item in output)
            visible_output = [item for item in output if item.get("type") != "message" or (
                item.get("phase") == "final_answer" if has_final else item.get("phase") != "commentary"
            )]
            turn = parse_response({**completed, "output": visible_output, "output_text": None})
            if emitted and (turn.tool_calls or not turn.text.startswith(emitted.strip())):
                raise _stream_error()
            return ModelTurn("" if turn.tool_calls else turn.text, turn.tool_calls, tuple(output), turn.usage)
        elif kind in ("response.failed", "error"):
            failure = payload.get("response") if kind == "response.failed" else payload
            raise _provider_stream_error(failure if isinstance(failure, dict) else {})
        elif kind == "response.incomplete":
            incomplete = payload.get("response")
            detail = incomplete.get("incomplete_details") if isinstance(incomplete, dict) else None
            if isinstance(detail, dict) and detail.get("reason") == "max_output_tokens":
                raise ModelError("model_output_truncated", "the model reached its output ceiling before it answered")
            raise _stream_error()
    # EOF is not completion, even if several plausible words have already arrived.
    raise _stream_error()


class ResponsesClient:
    """One HTTPS call per round, with the key held here and nowhere else."""

    def __init__(self, *, api_key: str | None, base_url: str, timeout_seconds: int) -> None:
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout_seconds

    @property
    def configured(self) -> bool:
        return bool(self._api_key)

    def respond(
        self,
        *,
        instructions: str,
        input_items: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        allow_tools: bool = True,
        on_text: Callable[[str], None] | None = None,
    ) -> ModelTurn:
        if not self._api_key:
            raise MissingApiKey()
        body = build_request(
            instructions=instructions,
            input_items=input_items,
            tools=tools,
            allow_tools=allow_tools,
        )
        if on_text is not None:
            body["stream"] = True
        request = urllib.request.Request(
            f"{self._base_url}/responses",
            data=json.dumps(body).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self._api_key}",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as response:
                if on_text is not None:
                    return _read_stream(response, on_text=on_text, allow_tools=allow_tools)
                payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            raise self._http_error(error) from None
        except urllib.error.URLError as error:
            reason = getattr(error, "reason", None)
            if isinstance(reason, TimeoutError):
                raise ModelError("model_timeout", "the model did not answer in time") from None
            raise ModelError("model_unavailable", "the model could not be reached") from None
        except TimeoutError:
            raise ModelError("model_timeout", "the model did not answer in time") from None
        except (json.JSONDecodeError, UnicodeDecodeError, http.client.IncompleteRead):
            raise ModelError(
                "model_unavailable", "the model returned an unreadable body"
            ) from None
        return parse_response(payload)

    @staticmethod
    def _http_error(error: urllib.error.HTTPError) -> ModelError:
        """Map the provider's status onto this service's own vocabulary.

        The provider's body is deliberately not carried through. It can quote the request
        back, and this endpoint answers anonymous callers -- so what reaches a visitor is
        a code and a sentence this repository wrote.

        One status cannot be classified without it. A 429 is either "you asked too often"
        or "there is no credit left", and those are opposite instructions: wait, or add
        money. The status is the same for both and only the body says which, so for a 429
        the body is opened, read for two words, and dropped.
        """
        status = getattr(error, "code", 0)
        if status in (401, 403):
            return ModelError(
                "demo_model_unconfigured", "the demo's model credential was not accepted"
            )
        if status == 429:
            provider_type, exhausted = _refusal(error)
            if exhausted:
                # Deliberately not "try again shortly". Waiting does not put credit on an
                # account, so every request after this one fails identically -- and a
                # visitor sent away to come back later is the one answer certain to be
                # wrong. The status this maps to says the same thing: see service.py.
                return ModelError(
                    "model_quota_exhausted",
                    "this demo is temporarily unavailable",
                    provider_type=provider_type,
                )
            return ModelError(
                "model_rate_limited",
                "the demo is busy; try again shortly",
                provider_type=provider_type,
            )
        if status in (408, 504):
            return ModelError("model_timeout", "the model did not answer in time")
        return ModelError("model_unavailable", "the model could not answer this turn")
