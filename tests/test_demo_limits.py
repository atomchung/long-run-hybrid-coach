"""What one anonymous request is allowed to cost, and what a full process does about it.

Every test here is a failure an independent review reproduced against this branch before
launch: a turn whose tool calls were unbounded, history trimmed into input the Responses
API rejects, a per-client limit defeated by a header the caller writes, and an eviction
that handed one conversation to two threads. They are behavioural, not structural -- each
drives the service the way a visitor would and checks what the model would have been sent.
"""

from __future__ import annotations

import datetime as dt
import json
import threading
import types
import unittest
from typing import Any
from unittest import mock

from entrypoints.demo import model as model_module, server as server_module, service as service_module
from entrypoints.demo.service import DemoRequestError, DemoService, _trimmed
from entrypoints.demo.config import DemoConfig


FAKE_CREDENTIAL = "demo-test-credential"
NOW = dt.datetime(2026, 9, 13, 10, 40, tzinfo=dt.timezone.utc)


class FakeModel:
    def __init__(self, *turns: model_module.ModelTurn) -> None:
        self.turns = list(turns)
        self.calls: list[dict[str, Any]] = []

    def respond(self, *, instructions, input_items, tools):
        self.calls.append({"input_items": [dict(item) for item in input_items]})
        return self.turns.pop(0) if len(self.turns) > 1 else self.turns[0]


def calls_turn(count: int, *, arguments: dict[str, Any] | None = None) -> model_module.ModelTurn:
    """One round in which the model asks for ``count`` evidence reads at once."""
    raws, calls = [], []
    for index in range(count):
        # Distinct by default, so the per-round cap is what this exercises rather than
        # the dedupe -- a visitor asking for "every group, one call each" gets forty
        # different calls, not forty copies of one.
        args = dict(arguments if arguments is not None else {"read": [f"group-{index}"]})
        call_id = f"call-{index}"
        raw = {
            "type": "function_call",
            "call_id": call_id,
            "name": "read_demo_evidence",
            "arguments": json.dumps(args),
        }
        raws.append(raw)
        calls.append(model_module.ToolCall(call_id, "read_demo_evidence", args, raw))
    return model_module.ModelTurn(text="", tool_calls=tuple(calls), output_items=tuple(raws))


def service(*turns: model_module.ModelTurn, **overrides: Any) -> tuple[DemoService, FakeModel]:
    model = FakeModel(*turns)
    demo = DemoService(
        DemoConfig(api_key=FAKE_CREDENTIAL, **overrides), client=model, now=lambda: NOW
    )
    return demo, model


def ask(demo: DemoService, session_id: str = "session-aaaaaaaa", message: str = "how is my week?",
        *, client_key: str = "203.0.113.7"):
    return demo.respond({"session_id": session_id, "message": message}, client_key=client_key)


def outputs(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [item for item in items if item.get("type") == "function_call_output"]


class ToolCallBudgetTest(unittest.TestCase):
    """The amplification: forty reads asked for in one round, each one carried forward."""

    def test_a_round_dispatches_at_most_the_cap_and_answers_the_rest(self):
        demo, model = service(calls_turn(40), model_module.ModelTurn(text="Here is the week."))
        ask(demo)

        sent = model.calls[1]["input_items"]
        results = outputs(sent)
        self.assertEqual(len(results), 40, "every function_call needs its output or the API 400s")
        budgeted = [item for item in results if "demo_call_budget" in json.dumps(item)]
        self.assertEqual(len(budgeted), 36)
        self.assertLessEqual(
            len(results) - len(budgeted), service_module.MAX_TOOL_CALLS_PER_ROUND
        )

    def test_the_carried_input_stays_bounded(self):
        demo, model = service(
            calls_turn(40), calls_turn(40), model_module.ModelTurn(text="Here is the week.")
        )
        ask(demo)
        largest = max(len(json.dumps(call["input_items"])) for call in model.calls)
        self.assertLess(largest, 256 * 1024, "one short request must not become megabytes")

    def test_the_same_read_twice_is_dispatched_once(self):
        demo, model = service(
            calls_turn(4, arguments={"read": ["recent_actuals"]}),
            model_module.ModelTurn(text="Here is the week."),
        )
        ask(demo)
        results = outputs(model.calls[1]["input_items"])
        self.assertEqual(len(results), 4)
        budgeted = [item for item in results if "demo_call_budget" in json.dumps(item)]
        self.assertEqual(len(budgeted), 3)


class HistoryTrimTest(unittest.TestCase):
    """Trimming shortens a conversation. It must not make it unanswerable."""

    def _exchange(self, index: int, *, padding: int) -> list[dict[str, Any]]:
        return [
            {"role": "user", "content": f"question {index}"},
            {"type": "function_call", "call_id": f"c{index}", "name": "read_demo_evidence",
             "arguments": "{}"},
            {"type": "function_call_output", "call_id": f"c{index}", "output": "x" * padding},
            {"role": "assistant", "content": f"answer {index}"},
        ]

    def test_no_tool_result_outlives_the_call_it_answers(self):
        items: list[dict[str, Any]] = []
        for index in range(6):
            items.extend(self._exchange(index, padding=30 * 1024))

        trimmed = _trimmed(list(items))

        calls = {item["call_id"] for item in trimmed if item.get("type") == "function_call"}
        for result in outputs(trimmed):
            self.assertIn(
                result["call_id"], calls,
                "a function_call_output whose call was trimmed away is input the API rejects",
            )

    def test_the_most_recent_exchange_survives_even_when_it_is_oversized(self):
        items = self._exchange(0, padding=200 * 1024)
        trimmed = _trimmed(list(items))
        self.assertEqual([item.get("role") for item in trimmed][0], "user")
        self.assertEqual(len(trimmed), 4)


class RotatedForwardedForTest(unittest.TestCase):
    """The per-client bucket is only worth having if the caller cannot choose its key."""

    def _key_for(self, header: str, **overrides: Any) -> str:
        demo, _ = service(model_module.ModelTurn(text="ok"), **overrides)
        handler = server_module.DemoHandler.__new__(server_module.DemoHandler)
        handler.headers = {"X-Forwarded-For": header}
        handler.client_address = ("10.0.0.9", 51234)
        handler.server = types.SimpleNamespace(service=demo)
        return handler._client_key()

    def test_the_proxys_entry_is_used_not_the_callers(self):
        self.assertEqual(self._key_for("1.2.3.4, 5.6.7.8, 198.51.100.4"), "198.51.100.4")

    def test_a_forged_header_cannot_buy_a_fresh_allowance(self):
        first = self._key_for("forged-one, 198.51.100.4")
        second = self._key_for("forged-two, 198.51.100.4")
        self.assertEqual(first, second)

    def test_peer_mode_ignores_the_header_entirely(self):
        self.assertEqual(
            self._key_for("1.2.3.4", client_ip_source="peer"), "10.0.0.9"
        )


class LimiterKeySpaceTest(unittest.TestCase):
    """A flood must not be able to allocate one counter per address it invents."""

    def test_keys_past_the_cap_share_one_bucket_and_are_refused(self):
        demo, _ = service(
            model_module.ModelTurn(text="ok"),
            requests_per_minute_per_client=2,
            requests_per_minute_global=100_000,
        )
        with mock.patch.object(service_module, "MAX_LIMITER_KEYS", 4):
            for index in range(4):
                ask(demo, f"session-{index:08d}", client_key=f"10.0.0.{index}")
            refused = 0
            for index in range(4, 20):
                try:
                    ask(demo, f"session-{index:08d}", client_key=f"10.0.1.{index}")
                except DemoRequestError as error:
                    self.assertEqual(error.code, "rate_limited")
                    refused += 1
        self.assertGreater(refused, 0, "rotating the key past the cap must stop being free")


class EvictionTest(unittest.TestCase):
    """A full store may take a conversation away. Never one that is mid-turn."""

    def test_a_session_answering_a_turn_is_not_evicted(self):
        demo, _ = service(model_module.ModelTurn(text="ok"), max_sessions=2)
        ask(demo, "session-busybus")
        ask(demo, "session-idleidl")
        busy = demo.sessions.get_or_create("session-busybus", now=NOW)
        self.assertTrue(busy.lock.acquire(blocking=False))
        try:
            ask(demo, "session-newcomer")
            self.assertIs(demo.sessions.get_or_create("session-busybus", now=NOW), busy)
        finally:
            busy.lock.release()

    def test_a_store_of_busy_conversations_refuses_rather_than_stealing_one(self):
        demo, _ = service(model_module.ModelTurn(text="ok"), max_sessions=1)
        held = demo.sessions.get_or_create("session-holdhold", now=NOW)
        self.assertTrue(held.lock.acquire(blocking=False))
        try:
            with self.assertRaises(DemoRequestError) as raised:
                ask(demo, "session-newcomer")
        finally:
            held.lock.release()
        self.assertEqual(raised.exception.status, 503)
        self.assertEqual(raised.exception.code, "demo_at_capacity")


class SocketTimeoutTest(unittest.TestCase):
    """A connection that says nothing must not hold a thread indefinitely."""

    def test_the_handler_sets_a_socket_timeout(self):
        self.assertIsNotNone(server_module.DemoHandler.timeout)
        self.assertLessEqual(server_module.DemoHandler.timeout, 30)


if __name__ == "__main__":
    unittest.main()
