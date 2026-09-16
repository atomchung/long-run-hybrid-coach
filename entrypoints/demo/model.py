"""The one outbound call this service makes: the OpenAI Responses API, on a pinned model.

Stdlib only, like the rest of the repository -- ``urllib.request`` rather than a vendor
SDK, so the demo image installs nothing and CI keeps running on a bare interpreter.

Three things are deliberate and none of them is configurable from a request:

*The model is pinned.* ``MODEL`` is a constant. There is no environment override and no
fallback to a smaller or older model when a call fails: a demo that quietly answers from a
different model is a demo whose answers mean nothing. A failure is reported as a failure.

*The key comes from the server's environment and goes nowhere else.* It is read once into
the configuration, sent only in the ``Authorization`` header of this request, and never
logged, never echoed into an error, and never included in any response this service
writes. ``tests/test_demo_service.py`` holds that.

*Nothing is stored at the provider.* ``store`` is false on every call, so a visitor's
sentence is not retained beyond answering it.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any


# Pinned. See the module note -- this is not a default, and there is no other value.
MODEL = "gpt-6-astra"

MAX_OUTPUT_TOKENS = 2000


class ModelError(Exception):
    """A call that did not produce an answer, with the code the client is told."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


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
class ModelTurn:
    """One response: the words, the acts it asked for, and the items to echo back."""

    text: str
    tool_calls: tuple[ToolCall, ...] = ()
    output_items: tuple[dict[str, Any], ...] = field(default=())


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
            arguments = json.loads(raw_arguments) if isinstance(raw_arguments, str) else raw_arguments
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


class ResponsesClient:
    """One HTTPS call per turn, with the key held here and nowhere else."""

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
    ) -> ModelTurn:
        if not self._api_key:
            raise MissingApiKey()
        body = {
            "model": MODEL,
            "instructions": instructions,
            "input": input_items,
            "max_output_tokens": MAX_OUTPUT_TOKENS,
            "store": False,
        }
        if tools:
            body["tools"] = tools
            body["tool_choice"] = "auto"
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
        except json.JSONDecodeError:
            raise ModelError("model_unavailable", "the model returned an unreadable body") from None
        text = _collect_text(payload)
        tool_calls = _collect_tool_calls(payload)
        if not text and not tool_calls:
            # Including a response cut short before it said anything. A reply truncated
            # partway through is kept as it is -- the words that did arrive are still an
            # answer, and discarding them would turn a long reply into an error page.
            raise ModelError("model_unavailable", "the model returned no answer")
        return ModelTurn(
            text=text,
            tool_calls=tool_calls,
            output_items=tuple(
                item for item in (payload.get("output") or []) if isinstance(item, dict)
            ),
        )

    @staticmethod
    def _http_error(error: urllib.error.HTTPError) -> ModelError:
        """Map the provider's status onto this service's own vocabulary.

        The provider's body is deliberately not carried through. It can quote the request
        back, and this endpoint answers anonymous callers -- so what reaches a visitor is
        a code and a sentence this repository wrote.
        """
        status = getattr(error, "code", 0)
        if status in (401, 403):
            return ModelError(
                "demo_model_unconfigured", "the demo's model credential was not accepted"
            )
        if status == 429:
            return ModelError("model_rate_limited", "the demo is busy; try again shortly")
        if status == 408 or status == 504:
            return ModelError("model_timeout", "the model did not answer in time")
        return ModelError("model_unavailable", "the model could not answer this turn")
