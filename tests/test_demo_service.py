"""What the public demo endpoint does with a request, and what it refuses to do.

Nothing here reaches a network. The model is a double in every test but one, and that one
patches ``urlopen`` -- so the suite still runs on a bare interpreter with no credential, as
the rest of this repository's does.

The tests are grouped by the thing that would go wrong in public: an unconfigured
deployment answering anyway, one visitor's conversation showing up in another's, a session
that never ends, a caller who does not stop, a model that asks for a write, a visitor told
to come back to a failure that will never clear, and a log line that writes down what
somebody typed.
"""

from __future__ import annotations

import datetime as dt
import io
import json
import logging
import threading
import unittest
import urllib.error
import urllib.request
from typing import Any
from unittest import mock

from entrypoints.demo import boundary, fixture, model as model_module, server as server_module
from entrypoints.demo.config import ConfigError, DemoConfig, SITE_ORIGIN, from_environment
from entrypoints.demo.service import (
    MODEL_QUOTA_STATES,
    DemoRequestError,
    DemoService,
    error_body,
)


# Short enough to read, long enough to be a real key. Never a real one.
FAKE_CREDENTIAL = "demo-test-credential"
NOW = dt.datetime(2026, 9, 13, 10, 40, tzinfo=dt.timezone.utc)


class FakeModel:
    """A model that says what the test told it to, and records what it was given.

    A ``ModelError`` among the turns is raised where it sits, so "refused, then answered"
    is written the same way two answers are, and the last turn repeats for every call
    after it.
    """

    def __init__(self, *turns: model_module.ModelTurn | model_module.ModelError) -> None:
        self.turns = list(turns) or [model_module.ModelTurn(text="A demo answer.")]
        self.calls: list[dict[str, Any]] = []

    def respond(self, *, instructions, input_items, tools):
        self.calls.append(
            {
                "instructions": instructions,
                "input_items": [dict(item) for item in input_items],
                "tools": tools,
            }
        )
        turn = self.turns.pop(0) if len(self.turns) > 1 else self.turns[0]
        if isinstance(turn, model_module.ModelError):
            raise turn
        return turn


def http_error(status: int, body: bytes | None = None) -> urllib.error.HTTPError:
    """A provider failure carrying the body it actually answered with, if any."""
    return urllib.error.HTTPError("u", status, "x", {}, io.BytesIO(body) if body else None)


def tool_turn(name: str, arguments: dict[str, Any], *, call_id: str = "call-1"):
    raw = {
        "type": "function_call",
        "call_id": call_id,
        "name": name,
        "arguments": json.dumps(arguments),
    }
    return model_module.ModelTurn(
        text="",
        tool_calls=(model_module.ToolCall(call_id, name, arguments, raw),),
        output_items=(raw,),
    )


def config(**overrides: Any) -> DemoConfig:
    return DemoConfig(api_key=FAKE_CREDENTIAL, **overrides)


def service(
    *turns: model_module.ModelTurn | model_module.ModelError, now=None, **overrides: Any
) -> DemoService:
    return DemoService(config(**overrides), client=FakeModel(*turns), now=now or (lambda: NOW))


def ask(demo: DemoService, session_id: str, message: str, *, client_key: str = "203.0.113.7"):
    return demo.respond({"session_id": session_id, "message": message}, client_key=client_key)


class MissingCredentialTest(unittest.TestCase):
    def test_the_service_does_not_start_without_a_credential(self):
        with self.assertRaises(server_module.StartupError) as raised:
            server_module.build_service(DemoConfig(api_key=None))
        self.assertIn("OPENAI_API_KEY", str(raised.exception))

    def test_starting_without_one_exits_non_zero_with_one_readable_line(self):
        from entrypoints.demo.__main__ import main

        with mock.patch.dict("os.environ", {"OPENAI_API_KEY": ""}, clear=False):
            with mock.patch("sys.stderr"):
                self.assertEqual(1, main())

    def test_a_request_to_an_unconfigured_deployment_is_answered_not_crashed(self):
        demo = DemoService(DemoConfig(api_key=None), client=FakeModel(), now=lambda: NOW)
        with self.assertRaises(DemoRequestError) as raised:
            ask(demo, "session-aaaaaaaa", "What should I do next week?")
        self.assertEqual(503, raised.exception.status)
        self.assertEqual("demo_model_unconfigured", raised.exception.code)

    def test_an_unconfigured_deployment_reports_itself_degraded(self):
        demo = DemoService(DemoConfig(api_key=None), client=FakeModel(), now=lambda: NOW)
        health = demo.health()
        self.assertEqual(503, health.status)
        self.assertEqual("absent", health.body["model_credential"])

    def test_a_credential_the_provider_rejects_does_not_become_a_different_model(self):
        rejected = model_module.ResponsesClient._http_error(
            urllib.error.HTTPError("u", 401, "no", {}, None)
        )
        self.assertEqual("demo_model_unconfigured", rejected.code)

    def test_the_client_refuses_rather_than_calling_without_a_key(self):
        client = model_module.ResponsesClient(
            api_key=None, base_url="https://example.invalid/v1", timeout_seconds=1
        )
        self.assertFalse(client.configured)
        with self.assertRaises(model_module.MissingApiKey):
            client.respond(instructions="x", input_items=[])


class PinnedModelTest(unittest.TestCase):
    def test_the_request_names_the_pinned_model_whatever_the_environment_says(self):
        captured: dict[str, Any] = {}

        class Response:
            def read(self):
                return json.dumps(
                    {"output": [{"type": "message", "content": [
                        {"type": "output_text", "text": "hello"}]}]}
                ).encode()

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        def fake_urlopen(request, timeout=None):
            captured["body"] = json.loads(request.data.decode())
            captured["headers"] = dict(request.header_items())
            captured["url"] = request.full_url
            return Response()

        client = model_module.ResponsesClient(
            api_key=FAKE_CREDENTIAL, base_url="https://api.openai.com/v1", timeout_seconds=5
        )
        with mock.patch.dict("os.environ", {"OPENAI_MODEL": "some-other-model"}, clear=False):
            with mock.patch("urllib.request.urlopen", fake_urlopen):
                turn = client.respond(instructions="be a coach", input_items=[
                    {"role": "user", "content": "hi"}])
        self.assertEqual("hello", turn.text)
        self.assertEqual("gpt-5.6-luna", model_module.MODEL)
        self.assertEqual("gpt-5.6-luna", captured["body"]["model"])
        self.assertFalse(captured["body"]["store"])
        self.assertTrue(captured["url"].endswith("/responses"))

    def test_the_credential_travels_only_in_the_authorization_header(self):
        captured: dict[str, Any] = {}

        class Response:
            def read(self):
                return json.dumps(
                    {"output": [{"type": "message", "content": [
                        {"type": "output_text", "text": "an answer"}]}]}
                ).encode()

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        def fake_urlopen(request, timeout=None):
            captured["body"] = request.data.decode()
            captured["headers"] = {k.lower(): v for k, v in request.header_items()}
            return Response()

        client = model_module.ResponsesClient(
            api_key=FAKE_CREDENTIAL, base_url="https://api.openai.com/v1", timeout_seconds=5
        )
        with mock.patch("urllib.request.urlopen", fake_urlopen):
            client.respond(instructions="x", input_items=[])
        self.assertIn(FAKE_CREDENTIAL, captured["headers"]["authorization"])
        self.assertNotIn(FAKE_CREDENTIAL, captured["body"])

    def test_a_response_with_nothing_in_it_is_an_error_rather_than_an_empty_reply(self):
        class Response:
            def read(self):
                return b'{"output": []}'

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        client = model_module.ResponsesClient(
            api_key=FAKE_CREDENTIAL, base_url="https://api.openai.com/v1", timeout_seconds=5
        )
        with mock.patch("urllib.request.urlopen", lambda request, timeout=None: Response()):
            with self.assertRaises(model_module.ModelError) as raised:
                client.respond(instructions="x", input_items=[])
        self.assertEqual("model_unavailable", raised.exception.code)

    def test_a_provider_failure_is_a_failure_rather_than_a_quieter_answer(self):
        for status, code in ((429, "model_rate_limited"), (500, "model_unavailable"),
                             (504, "model_timeout")):
            with self.subTest(status=status):
                error = model_module.ResponsesClient._http_error(
                    urllib.error.HTTPError("u", status, "x", {}, None)
                )
                self.assertEqual(code, error.code)


class AcceptanceTurnTest(unittest.TestCase):
    """The three turns this demo exists to answer."""

    def test_each_acceptance_prompt_is_accepted_and_answered(self):
        for prompt in fixture.acceptance_prompts():
            with self.subTest(prompt=prompt["id"]):
                demo = service(model_module.ModelTurn(text="Three options, with costs."))
                reply = ask(demo, f"acceptance-{prompt['id']}"[:60], prompt["message"])
                self.assertEqual(200, reply.status)
                self.assertEqual({"reply": "Three options, with costs."}, reply.body)

    def test_the_model_is_handed_the_product_s_own_training_judgment(self):
        from garmin_coach_loop import orchestration

        demo = service()
        instructions = demo.instructions()
        self.assertIn(orchestration.training_judgment(), instructions)

    def test_the_model_is_handed_the_allocation_contract_the_hero_turn_needs(self):
        instructions = service().instructions()
        for word in ("Preserve", "Sacrifice", "Evidence", "Uncertainty"):
            self.assertIn(word, instructions)
        self.assertIn("two or three materially different allocations", instructions)

    def test_the_model_is_told_not_to_invent_a_score(self):
        instructions = service().instructions()
        self.assertIn("running score, strength score, recovery score", instructions)

    def test_the_model_is_handed_the_athlete_s_read_and_what_it_left_out(self):
        read = json.loads(service().instructions().split("```json\n")[1].split("\n```")[0])
        self.assertTrue(read["plan_state"]["present"])
        self.assertEqual("demo-plan-001", read["plan_state"]["plan_id"])
        self.assertIn("constraints", read["context"])
        self.assertIn("evidence_index", read)
        self.assertIn("read_more", read["evidence_index"])

    def test_the_evidence_the_second_acceptance_turn_needs_is_one_read_away(self):
        # The comparable execution trend lives in segment_execution, which the default read
        # deliberately leaves out -- the demo has to ask for it, exactly as a client does.
        # The index still names it, which is how the model knows the question is answerable.
        read = json.loads(service().instructions().split("```json\n")[1].split("\n```")[0])
        self.assertNotIn("segment_execution", read["context"])
        self.assertIn("session_detail", json.dumps(read["evidence_index"]))
        result = boundary.dispatch(
            boundary.READ_EVIDENCE,
            {"read": ["session_detail"]},
            before=fixture.plan_state(),
            issued_at=NOW,
        )
        self.assertIn("segment_execution", result["evidence"])

    def test_the_declared_measurement_and_the_execution_trend_are_separate_fields(self):
        read = json.loads(service().instructions().split("```json\n")[1].split("\n```")[0])
        evidence = read["context"]["measurement_evidence"]
        self.assertEqual("attached", evidence["reference_result"])
        self.assertNotEqual("attached", evidence["comparison_result"])


class SessionIsolationTest(unittest.TestCase):
    def test_two_sessions_share_no_history(self):
        demo = service()
        ask(demo, "session-one-aaaa", "I only have three 45-minute sessions next week.")
        ask(demo, "session-two-bbbb", "What changed across the comparable quality runs?")
        first = demo.sessions.get_or_create("session-one-aaaa", now=NOW)
        second = demo.sessions.get_or_create("session-two-bbbb", now=NOW)
        self.assertEqual(1, len(demo._client.calls[0]["input_items"]))
        for item in second.history:
            self.assertNotIn("45-minute", json.dumps(item))
        self.assertIsNot(first.plan_state, second.plan_state)
        self.assertIsNot(first.history, second.history)

    def test_a_preview_in_one_session_reaches_no_other_and_not_the_fixture(self):
        from tests.test_demo_boundary import CHANGE_REQUEST

        demo = service(
            tool_turn(boundary.PREVIEW_PLAN_CHANGE, {"change_request": CHANGE_REQUEST}),
            model_module.ModelTurn(text="Here is what that would look like."),
        )
        ask(demo, "session-preview-1", "Thursday is no longer available.")
        first = demo.sessions.get_or_create("session-preview-1", now=NOW)
        self.assertEqual(1, len(first.previews))

        second = demo.sessions.get_or_create("session-preview-2", now=NOW)
        self.assertEqual([], second.previews)
        self.assertEqual(fixture.plan_state(), second.plan_state)
        # The base fixture on disk is what it always was.
        self.assertEqual(fixture.plan_state(), first.plan_state)

    def test_the_store_is_bounded_and_evicts_the_least_recently_touched(self):
        demo = service(max_sessions=2)
        for index in range(3):
            ask(demo, f"session-bound-{index}", "hello", client_key=f"198.51.100.{index}")
        self.assertEqual(2, len(demo.sessions))


class SessionLifetimeTest(unittest.TestCase):
    def test_a_session_expires_and_comes_back_as_a_new_conversation(self):
        clock = {"now": NOW}
        demo = service(session_ttl_seconds=60, now=lambda: clock["now"])
        ask(demo, "session-ttl-aaaa", "first turn")
        self.assertEqual(1, len(demo.sessions))

        clock["now"] = NOW + dt.timedelta(seconds=61)
        self.assertEqual(1, demo.purge_expired())
        self.assertEqual(0, len(demo.sessions))

        ask(demo, "session-ttl-aaaa", "second turn")
        revived = demo.sessions.get_or_create("session-ttl-aaaa", now=clock["now"])
        self.assertEqual(1, revived.turns, "an expired id resumes nothing")

    def test_a_session_is_limited_to_its_turns(self):
        demo = service(max_turns_per_session=2)
        ask(demo, "session-turns-aaa", "one")
        ask(demo, "session-turns-aaa", "two")
        with self.assertRaises(DemoRequestError) as raised:
            ask(demo, "session-turns-aaa", "three")
        self.assertEqual(409, raised.exception.status)
        self.assertEqual("turn_limit_reached", raised.exception.code)

    def test_the_turn_limit_is_per_session_not_per_caller(self):
        demo = service(max_turns_per_session=1)
        ask(demo, "session-turns-one", "one")
        self.assertEqual(200, ask(demo, "session-turns-two", "one").status)


class RequestShapeTest(unittest.TestCase):
    def test_a_message_longer_than_the_limit_is_refused(self):
        demo = service(max_message_chars=40)
        with self.assertRaises(DemoRequestError) as raised:
            ask(demo, "session-long-aaaa", "x" * 41)
        self.assertEqual(400, raised.exception.status)
        self.assertEqual("message_too_long", raised.exception.code)

    def test_an_empty_or_missing_message_is_refused(self):
        demo = service()
        for message in ("", "   ", None, 7):
            with self.subTest(message=message):
                with self.assertRaises(DemoRequestError) as raised:
                    demo.respond(
                        {"session_id": "session-shape-aa", "message": message},
                        client_key="203.0.113.7",
                    )
                self.assertEqual("invalid_request", raised.exception.code)

    def test_a_session_id_that_is_not_an_opaque_token_is_refused(self):
        demo = service()
        for session_id in ("short", "x" * 129, "has space", "../../etc/passwd", None, 12):
            with self.subTest(session_id=session_id):
                with self.assertRaises(DemoRequestError) as raised:
                    demo.respond(
                        {"session_id": session_id, "message": "hello"},
                        client_key="203.0.113.7",
                    )
                self.assertEqual("session_id_invalid", raised.exception.code)

    def test_an_unexpected_field_is_refused_rather_than_ignored(self):
        demo = service()
        with self.assertRaises(DemoRequestError) as raised:
            demo.respond(
                {"session_id": "session-extra-aa", "message": "hi", "mcp_url": "https://x"},
                client_key="203.0.113.7",
            )
        self.assertEqual("invalid_request", raised.exception.code)
        self.assertIn("mcp_url", raised.exception.message)


class RateLimitTest(unittest.TestCase):
    def test_one_caller_is_limited_per_minute(self):
        demo = service(requests_per_minute_per_client=2)
        for index in range(2):
            ask(demo, f"session-rate-{index}", "hello", client_key="203.0.113.9")
        with self.assertRaises(DemoRequestError) as raised:
            ask(demo, "session-rate-3", "hello", client_key="203.0.113.9")
        self.assertEqual(429, raised.exception.status)
        self.assertEqual("rate_limited", raised.exception.code)
        self.assertIsNotNone(raised.exception.retry_after)

    def test_another_caller_is_not_limited_by_the_first(self):
        demo = service(requests_per_minute_per_client=1)
        ask(demo, "session-rate-aaa", "hello", client_key="203.0.113.9")
        self.assertEqual(
            200, ask(demo, "session-rate-bbb", "hello", client_key="203.0.113.10").status
        )

    def test_the_whole_process_has_a_ceiling_a_rotated_address_cannot_pass(self):
        demo = service(requests_per_minute_global=2, requests_per_minute_per_client=100)
        for index in range(2):
            ask(demo, f"session-global-{index}", "hello", client_key=f"203.0.113.{index}")
        with self.assertRaises(DemoRequestError) as raised:
            ask(demo, "session-global-9", "hello", client_key="203.0.113.99")
        self.assertEqual(429, raised.exception.status)


class ForbiddenWritePathTest(unittest.TestCase):
    def test_a_model_asking_for_a_write_is_refused_and_told_it_is_a_playground(self):
        demo = service(
            tool_turn("applyCoachDecision", {"proposal": "anything"}),
            model_module.ModelTurn(text="I cannot save that here."),
        )
        reply = ask(demo, "session-write-aaa", "Just put it on my calendar.")
        self.assertEqual(200, reply.status)
        self.assertEqual(["applyCoachDecision"], reply.log["refused"])

        sent = demo._client.calls[-1]["input_items"]
        outputs = [item for item in sent if item.get("type") == "function_call_output"]
        result = json.loads(outputs[0]["output"])
        self.assertEqual("demo_write_forbidden", result["code"])
        self.assertIn("playground", result["playground"])

    def test_a_refused_write_leaves_the_session_plan_exactly_as_it_was(self):
        demo = service(
            tool_turn("applyWorkoutDelivery", {}),
            model_module.ModelTurn(text="No."),
        )
        ask(demo, "session-write-bbb", "deliver it")
        session = demo.sessions.get_or_create("session-write-bbb", now=NOW)
        self.assertEqual(fixture.plan_state(), session.plan_state)
        self.assertEqual([], session.previews)

    def test_only_the_two_allowed_acts_are_offered_to_the_model(self):
        demo = service()
        ask(demo, "session-tools-aaa", "hello")
        offered = {tool["name"] for tool in demo._client.calls[0]["tools"]}
        self.assertEqual({boundary.READ_EVIDENCE, boundary.PREVIEW_PLAN_CHANGE}, offered)


class QuotaRefusalTest(unittest.TestCase):
    """The 429 that never clears, told apart from the one that does.

    Both arrive with the same status and only the body says which is which, so the body is
    read to classify. The tests below are what stops that reading from turning into a
    provider body reaching a visitor: the code differs, the sentence differs, the log
    carries the provider's one word and none of its prose, and ``/healthz`` says which of
    the two questions about a credential -- set, or able to pay -- is now answered no.

    The quota body is the one quoted in issue #476, observed by calling the API outside the
    service. Both shapes are pinned because the report quotes it flat and the provider
    nests it under ``error``.
    """

    QUOTA = json.dumps(
        {
            "type": "insufficient_quota",
            "code": "credit_balance_exhausted",
            "message": "You have no credits remaining. Add credits to keep going.",
        }
    ).encode()
    NESTED_QUOTA = json.dumps(
        {
            "error": {
                "type": "insufficient_quota",
                "code": "credit_balance_exhausted",
                "message": "You have no credits remaining. Add credits to keep going.",
            }
        }
    ).encode()
    RATE_LIMIT = json.dumps(
        {
            "error": {
                "type": "rate_limit_error",
                "code": "rate_limit_exceeded",
                "message": "Rate limit reached. Please try again in 1.3s.",
            }
        }
    ).encode()

    def refusal(self) -> model_module.ModelError:
        return model_module.ResponsesClient._http_error(http_error(429, self.QUOTA))

    # ------------------------------------------------------------------- the two shapes
    def test_a_429_that_means_no_credit_is_not_a_rate_limit(self):
        # The third shape is the same body with a message long enough to reach the read
        # limit: a truncated body parses as nothing and would read as a rate limit again.
        wordy = json.dumps(
            {
                "type": "insufficient_quota",
                "code": "credit_balance_exhausted",
                "message": "You have no credits remaining. " + "Add credits. " * 400,
            }
        ).encode()
        for shape, body in (
            ("flat", self.QUOTA),
            ("nested", self.NESTED_QUOTA),
            ("wordy", wordy),
        ):
            with self.subTest(shape=shape):
                error = model_module.ResponsesClient._http_error(http_error(429, body))
                self.assertEqual("model_quota_exhausted", error.code)
                self.assertEqual("insufficient_quota", error.provider_type)

    def test_a_429_that_really_is_a_rate_limit_keeps_its_own_code_and_sentence(self):
        error = model_module.ResponsesClient._http_error(http_error(429, self.RATE_LIMIT))
        self.assertEqual("model_rate_limited", error.code)
        self.assertIn("try again shortly", str(error))
        self.assertEqual("rate_limit_error", error.provider_type)

    def test_a_429_whose_body_says_nothing_readable_stays_a_rate_limit(self):
        for body in (None, b"", b"<html>slow down</html>", b'"a string"'):
            with self.subTest(body=body):
                error = model_module.ResponsesClient._http_error(http_error(429, body))
                self.assertEqual("model_rate_limited", error.code)
                self.assertIsNone(error.provider_type)

    def test_the_visitor_is_not_sent_back_to_a_failure_that_will_not_clear(self):
        self.assertNotIn("try again", str(self.refusal()).lower())
        self.assertIn("unavailable", str(self.refusal()))

    # ------------------------------------------------------------------------ the turn
    def test_the_turn_is_refused_503_without_a_retry_after(self):
        demo = service(self.refusal())
        with self.assertRaises(DemoRequestError) as raised:
            ask(demo, "session-quota-aaa", "What should I do this week?")
        self.assertEqual(503, raised.exception.status)
        self.assertEqual("model_quota_exhausted", raised.exception.code)
        self.assertIsNone(raised.exception.retry_after)
        self.assertNotIn("try again", raised.exception.message.lower())

    def test_nothing_the_provider_wrote_reaches_the_visitor(self):
        demo = service(self.refusal())
        with self.assertRaises(DemoRequestError) as raised:
            ask(demo, "session-quota-bbb", "What should I do this week?")
        written = json.dumps(error_body(raised.exception))
        self.assertNotIn("credits remaining", written)
        self.assertNotIn("insufficient_quota", written)
        self.assertNotIn("credit_balance_exhausted", written)

    # ------------------------------------------------------------------------- the log
    def test_the_log_names_this_code_and_the_provider_s_type_and_nothing_else(self):
        demo = service(self.refusal())
        with self.assertLogs("entrypoints.demo", level="INFO") as captured:
            with self.assertRaises(DemoRequestError):
                ask(demo, "session-quota-ccc", "What should I do this week?")
        logged = [json.loads(record.getMessage()) for record in captured.records]
        refused = [entry for entry in logged if entry.get("event") == "model_refused"]
        self.assertEqual(1, len(refused))
        self.assertEqual({"event", "session", "code", "provider_type"}, set(refused[0]))
        self.assertEqual("model_quota_exhausted", refused[0]["code"])
        self.assertEqual("insufficient_quota", refused[0]["provider_type"])

        written = "\n".join(captured.output)
        self.assertNotIn("credits remaining", written)
        self.assertNotIn("credit_balance_exhausted", written)
        self.assertNotIn(FAKE_CREDENTIAL, written)
        self.assertNotIn("session-quota-ccc", written)

    def test_a_real_rate_limit_is_logged_as_one_rather_than_as_a_quota_refusal(self):
        limited = model_module.ResponsesClient._http_error(http_error(429, self.RATE_LIMIT))
        demo = service(limited)
        with self.assertLogs("entrypoints.demo", level="INFO") as captured:
            with self.assertRaises(DemoRequestError) as raised:
                ask(demo, "session-quota-ddd", "What should I do this week?")
        self.assertEqual(429, raised.exception.status)
        logged = [json.loads(record.getMessage()) for record in captured.records]
        refused = [entry for entry in logged if entry.get("event") == "model_refused"]
        self.assertEqual(["model_rate_limited"], [entry["code"] for entry in refused])
        self.assertEqual(["rate_limit_error"], [entry["provider_type"] for entry in refused])

    # ---------------------------------------------------------------------- the health
    def test_health_tells_a_credential_that_is_set_from_one_that_cannot_pay(self):
        demo = service(self.refusal())
        self.assertEqual("unknown", demo.health().body["model_quota"])
        self.assertEqual(200, demo.health().status)

        with self.assertRaises(DemoRequestError):
            ask(demo, "session-quota-eee", "What should I do this week?")

        health = demo.health()
        self.assertEqual("present", health.body["model_credential"])
        self.assertEqual("exhausted", health.body["model_quota"])
        self.assertEqual("degraded", health.body["status"])
        self.assertEqual(503, health.status)

    def test_a_rate_limit_says_nothing_about_the_account_s_credit(self):
        limited = model_module.ResponsesClient._http_error(http_error(429, self.RATE_LIMIT))
        demo = service(limited)
        with self.assertRaises(DemoRequestError):
            ask(demo, "session-quota-fff", "What should I do this week?")
        self.assertEqual("unknown", demo.health().body["model_quota"])
        self.assertEqual(200, demo.health().status)

    def test_the_next_call_that_answers_clears_the_refusal(self):
        demo = service(self.refusal(), model_module.ModelTurn(text="A demo answer."))
        with self.assertRaises(DemoRequestError):
            ask(demo, "session-quota-ggg", "What should I do this week?")
        self.assertEqual("exhausted", demo.health().body["model_quota"])

        ask(demo, "session-quota-ggg", "And after that?")
        self.assertEqual("ok", demo.health().body["model_quota"])
        self.assertEqual(200, demo.health().status)

    def test_health_asks_the_provider_nothing(self):
        fake = FakeModel()
        demo = DemoService(config(), client=fake, now=lambda: NOW)
        demo.health()
        demo.health()
        self.assertEqual([], fake.calls)

    def test_the_health_vocabulary_is_a_fixed_set_of_words(self):
        demo = service(self.refusal(), model_module.ModelTurn(text="A demo answer."))
        seen = [demo.health().body["model_quota"]]
        with self.assertRaises(DemoRequestError):
            ask(demo, "session-quota-hhh", "What should I do this week?")
        seen.append(demo.health().body["model_quota"])
        ask(demo, "session-quota-hhh", "And after that?")
        seen.append(demo.health().body["model_quota"])
        self.assertEqual(["unknown", "exhausted", "ok"], seen)
        self.assertEqual(sorted(MODEL_QUOTA_STATES), sorted(set(seen)))


class PrivacySafeLogTest(unittest.TestCase):
    def test_the_access_log_carries_no_message_no_athlete_and_no_credential(self):
        demo = service()
        secret_message = "My name is on my watch and my resting heart rate is 47."
        with self.assertLogs("entrypoints.demo", level="INFO") as captured:
            reply = ask(demo, "session-logged-aa", secret_message)
            logging.getLogger("entrypoints.demo").info(json.dumps(reply.log))
        written = "\n".join(captured.output)
        self.assertNotIn(secret_message, written)
        self.assertNotIn(FAKE_CREDENTIAL, written)
        self.assertNotIn("session-logged-aa", written)
        self.assertNotIn("demo-plan-001", written)

    def test_the_log_entry_identifies_the_session_only_by_fingerprint(self):
        demo = service()
        reply = ask(demo, "session-fingerprint", "hello")
        self.assertNotIn("session-fingerprint", json.dumps(reply.log))
        self.assertEqual(12, len(reply.log["session"]))

    def test_the_same_session_fingerprints_the_same_way_every_time(self):
        demo = service()
        first = ask(demo, "session-stable-aaa", "one").log["session"]
        second = ask(demo, "session-stable-aaa", "two").log["session"]
        self.assertEqual(first, second)


class ConfigurationTest(unittest.TestCase):
    def test_the_site_origin_is_always_allowed_and_a_wildcard_never_is(self):
        self.assertIn(SITE_ORIGIN, from_environment({}).allowed_origins)
        with self.assertRaises(ConfigError):
            from_environment({"COACH_DEMO_ALLOWED_ORIGINS": "*"})

    def test_a_preview_origin_may_be_added_but_only_over_https_or_loopback(self):
        allowed = from_environment(
            {"COACH_DEMO_ALLOWED_ORIGINS": "https://preview.example,http://localhost:4321"}
        ).allowed_origins
        self.assertIn("https://preview.example", allowed)
        self.assertIn("http://localhost:4321", allowed)
        with self.assertRaises(ConfigError):
            from_environment({"COACH_DEMO_ALLOWED_ORIGINS": "http://preview.example"})

    def test_the_credential_is_read_from_the_server_environment_only(self):
        self.assertIsNone(from_environment({}).api_key)
        self.assertEqual(
            FAKE_CREDENTIAL, from_environment({"OPENAI_API_KEY": FAKE_CREDENTIAL}).api_key
        )

    def test_a_limit_that_is_not_a_number_stops_the_deployment(self):
        with self.assertRaises(ConfigError):
            from_environment({"COACH_DEMO_MAX_TURNS": "lots"})

    def test_a_model_base_url_that_is_not_https_is_refused(self):
        with self.assertRaises(ConfigError):
            from_environment({"COACH_DEMO_MODEL_BASE_URL": "http://api.example"})


class HttpSurfaceTest(unittest.TestCase):
    """The transport, against a real socket."""

    @classmethod
    def setUpClass(cls):
        cls.service = DemoService(
            config(allowed_origins=(SITE_ORIGIN, "https://preview.example")),
            client=FakeModel(),
            now=lambda: NOW,
        )
        cls.server = server_module.DemoServer(("127.0.0.1", 0), cls.service)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)

    def _post(self, body, *, headers=None, raw=None, path="/demo/v1/respond"):
        data = raw if raw is not None else json.dumps(body).encode()
        request = urllib.request.Request(
            self.base + path,
            data=data,
            headers={"Content-Type": "application/json", **(headers or {})},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request) as response:
                return response.status, json.loads(response.read()), dict(response.headers)
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read()), dict(error.headers)

    def test_a_turn_answers_with_a_reply_and_nothing_else(self):
        status, body, headers = self._post(
            {"session_id": "http-session-001", "message": "What are my options?"},
            headers={"Origin": SITE_ORIGIN},
        )
        self.assertEqual(200, status)
        self.assertEqual(["reply"], list(body))
        self.assertEqual(SITE_ORIGIN, headers["Access-Control-Allow-Origin"])
        self.assertEqual("no-store", headers["Cache-Control"])

    def test_an_origin_that_is_not_allowed_is_refused(self):
        status, body, headers = self._post(
            {"session_id": "http-session-002", "message": "hello"},
            headers={"Origin": "https://not-the-site.example"},
        )
        self.assertEqual(403, status)
        self.assertEqual("origin_not_allowed", body["error"]["code"])
        self.assertNotIn("Access-Control-Allow-Origin", headers)

    def test_the_preflight_answers_for_an_allowed_origin_only(self):
        def preflight(origin):
            request = urllib.request.Request(
                self.base + "/demo/v1/respond",
                headers={"Origin": origin, "Access-Control-Request-Method": "POST"},
                method="OPTIONS",
            )
            try:
                with urllib.request.urlopen(request) as response:
                    return response.status, dict(response.headers)
            except urllib.error.HTTPError as error:
                return error.code, dict(error.headers)

        status, headers = preflight("https://preview.example")
        self.assertEqual(204, status)
        self.assertEqual("https://preview.example", headers["Access-Control-Allow-Origin"])
        self.assertEqual("Origin", headers["Vary"])

        status, headers = preflight("https://not-the-site.example")
        self.assertEqual(403, status)
        self.assertNotIn("Access-Control-Allow-Origin", headers)

    def test_a_body_larger_than_the_limit_is_refused_before_it_is_read(self):
        status, body, _ = self._post(
            None, raw=b"x" * (self.service.config.max_body_bytes + 1)
        )
        self.assertEqual(413, status)
        self.assertEqual("payload_too_large", body["error"]["code"])

    def test_a_file_upload_has_nowhere_to_go(self):
        status, body, _ = self._post(
            None,
            raw=b"--x\r\nContent-Disposition: form-data; name=f; filename=a.fit\r\n\r\n..\r\n--x--",
            headers={"Content-Type": "multipart/form-data; boundary=x"},
        )
        self.assertEqual(415, status)
        self.assertEqual("unsupported_media_type", body["error"]["code"])

    def test_a_body_that_is_not_json_is_refused(self):
        status, body, _ = self._post(None, raw=b"not json at all")
        self.assertEqual(400, status)
        self.assertEqual("invalid_request", body["error"]["code"])

    def test_an_unknown_route_answers_a_machine_readable_error(self):
        status, body, _ = self._post(
            {"session_id": "http-session-003", "message": "hi"}, path="/mcp"
        )
        self.assertEqual(404, status)
        self.assertEqual("not_found", body["error"]["code"])

    def test_health_says_whether_this_deployment_can_answer(self):
        with urllib.request.urlopen(self.base + "/healthz") as response:
            body = json.loads(response.read())
        self.assertEqual("ok", body["status"])
        self.assertEqual("gpt-5.6-luna", body["model"])
        self.assertEqual("present", body["model_credential"])
        # Whether a credential can pay is a second question, answered over the same wire.
        # Which of the two non-refused words shows depends on whether a turn in this class
        # has run yet; the one that matters to a deploy check is the one that must not.
        self.assertIn(body["model_quota"], MODEL_QUOTA_STATES)
        self.assertNotEqual("exhausted", body["model_quota"])
        self.assertNotIn(FAKE_CREDENTIAL, json.dumps(body))


class ConcurrentTurnTest(unittest.TestCase):
    """Two requests on one session id must not each write back a history the other never saw."""

    def test_a_second_turn_on_the_same_session_is_refused_while_the_first_is_running(self):
        started = threading.Event()
        release = threading.Event()

        class Slow:
            def respond(self, *, instructions, input_items, tools):
                started.set()
                release.wait(timeout=5)
                return model_module.ModelTurn(text="first answer")

        demo = DemoService(config(), client=Slow(), now=lambda: NOW)
        outcome: dict[str, Any] = {}

        def first():
            outcome["first"] = ask(demo, "session-concurrent", "one").status

        worker = threading.Thread(target=first)
        worker.start()
        self.assertTrue(started.wait(timeout=5))
        with self.assertRaises(DemoRequestError) as raised:
            ask(demo, "session-concurrent", "two", client_key="198.51.100.2")
        self.assertEqual(409, raised.exception.status)
        self.assertEqual("session_busy", raised.exception.code)
        release.set()
        worker.join(timeout=5)
        self.assertEqual(200, outcome["first"])

    def test_the_refused_second_turn_leaves_one_clean_history_behind(self):
        started = threading.Event()
        release = threading.Event()

        class Slow:
            def respond(self, *, instructions, input_items, tools):
                started.set()
                release.wait(timeout=5)
                return model_module.ModelTurn(
                    text="first answer",
                    output_items=({"type": "message", "role": "assistant", "content": [
                        {"type": "output_text", "text": "first answer"}]},),
                )

        demo = DemoService(config(), client=Slow(), now=lambda: NOW)
        worker = threading.Thread(target=lambda: ask(demo, "session-clean-hist", "one"))
        worker.start()
        self.assertTrue(started.wait(timeout=5))
        with self.assertRaises(DemoRequestError):
            ask(demo, "session-clean-hist", "two", client_key="198.51.100.3")
        release.set()
        worker.join(timeout=5)

        session = demo.sessions.get_or_create("session-clean-hist", now=NOW)
        self.assertEqual(1, session.turns, "the refused turn spent nothing")
        said = [item for item in session.history if item.get("role") == "user"]
        self.assertEqual(["one"], [item["content"] for item in said])

    def test_two_different_sessions_still_run_at_the_same_time(self):
        started = threading.Event()
        release = threading.Event()

        class Slow:
            def respond(self, *, instructions, input_items, tools):
                started.set()
                release.wait(timeout=5)
                return model_module.ModelTurn(text="answer")

        demo = DemoService(config(), client=Slow(), now=lambda: NOW)
        worker = threading.Thread(target=lambda: ask(demo, "session-parallel-a", "one"))
        worker.start()
        self.assertTrue(started.wait(timeout=5))
        release.set()
        worker.join(timeout=5)
        self.assertEqual(
            200, ask(demo, "session-parallel-b", "one", client_key="198.51.100.4").status
        )


class FailedTurnQuotaTest(unittest.TestCase):
    """A turn the visitor never got an answer to does not come out of their few."""

    class Broken:
        def __init__(self, error: model_module.ModelError):
            self.error = error
            self.calls = 0

        def respond(self, *, instructions, input_items, tools):
            self.calls += 1
            raise self.error

    def test_a_provider_failure_gives_the_turn_back(self):
        for code, status in (
            ("model_timeout", 504),
            ("model_unavailable", 502),
            ("model_rate_limited", 429),
            ("model_quota_exhausted", 503),
            ("model_output_truncated", 502),
        ):
            with self.subTest(code=code):
                broken = self.Broken(model_module.ModelError(code, "no answer"))
                demo = DemoService(config(max_turns_per_session=2), client=broken,
                                   now=lambda: NOW)
                with self.assertRaises(DemoRequestError) as raised:
                    ask(demo, f"session-refund-{code}"[:60], "hello")
                self.assertEqual(status, raised.exception.status)
                session = demo.sessions.get_or_create(f"session-refund-{code}"[:60], now=NOW)
                self.assertEqual(0, session.turns)

    def test_a_failed_turn_writes_nothing_into_the_conversation(self):
        broken = self.Broken(model_module.ModelError("model_timeout", "no answer"))
        demo = DemoService(config(), client=broken, now=lambda: NOW)
        with self.assertRaises(DemoRequestError):
            ask(demo, "session-refund-hist", "a question nobody answered")
        session = demo.sessions.get_or_create("session-refund-hist", now=NOW)
        self.assertEqual([], session.history)

    def test_the_conversation_can_still_be_spent_by_turns_that_did_answer(self):
        demo = service(max_turns_per_session=1)
        ask(demo, "session-spent-aaa", "one")
        with self.assertRaises(DemoRequestError) as raised:
            ask(demo, "session-spent-aaa", "two")
        self.assertEqual("turn_limit_reached", raised.exception.code)

    def test_a_failure_part_way_through_a_tool_round_also_gives_the_turn_back(self):
        class FailsSecondRound:
            def __init__(self):
                self.calls = 0

            def respond(self, *, instructions, input_items, tools):
                self.calls += 1
                if self.calls == 1:
                    return tool_turn(boundary.READ_EVIDENCE, {"read": ["strength"]})
                raise model_module.ModelError("model_timeout", "no answer")

        demo = DemoService(config(), client=FailsSecondRound(), now=lambda: NOW)
        with self.assertRaises(DemoRequestError):
            ask(demo, "session-refund-mid", "what changed?")
        session = demo.sessions.get_or_create("session-refund-mid", now=NOW)
        self.assertEqual(0, session.turns)
        self.assertEqual([], session.history)


class AcceptanceCommandTest(unittest.TestCase):
    """The command an operator runs once there is a credential, exercised without one."""

    def test_it_refuses_to_run_in_process_without_a_credential(self):
        from entrypoints.demo import acceptance

        with mock.patch.dict("os.environ", {"OPENAI_API_KEY": ""}, clear=False):
            with mock.patch("sys.stderr"):
                self.assertEqual(2, acceptance.main([]))

    def test_it_runs_the_three_committed_turns_in_three_separate_conversations(self):
        from entrypoints.demo import acceptance

        seen: list[str] = []

        class Recording:
            def respond(self, *, instructions, input_items, tools):
                seen.append(
                    next(item["content"] for item in input_items if item.get("role") == "user")
                )
                return model_module.ModelTurn(
                    text="Preserve the anchor, sacrifice the long run. Evidence: three "
                         "sessions. Uncertain: the measurement is unproven; I would give up "
                         "the easy run."
                )

        demo = DemoService(config(), client=Recording(), now=lambda: NOW)
        report = acceptance.run(acceptance._InProcess(demo))
        self.assertEqual(3, len(report["turns"]))
        self.assertEqual([prompt["message"] for prompt in fixture.acceptance_prompts()], seen)
        self.assertEqual(3, len(demo.sessions), "one conversation per acceptance turn")
        self.assertTrue(report["passed"])

    def test_a_reply_claiming_a_write_fails_the_run(self):
        from entrypoints.demo import acceptance

        class Claims:
            def respond(self, *, instructions, input_items, tools):
                return model_module.ModelTurn(text="Done — I've saved that to your calendar.")

        demo = DemoService(config(), client=Claims(), now=lambda: NOW)
        report = acceptance.run(acceptance._InProcess(demo))
        self.assertFalse(report["passed"])
        self.assertTrue(any("claims_no_write" in name for name in report["hard_failures"]))

    def test_a_reply_inventing_a_score_fails_the_run(self):
        from entrypoints.demo import acceptance

        class Scores:
            def respond(self, *, instructions, input_items, tools):
                return model_module.ModelTurn(text="Your running score is 74 this week.")

        demo = DemoService(config(), client=Scores(), now=lambda: NOW)
        report = acceptance.run(acceptance._InProcess(demo))
        self.assertFalse(report["passed"])
        self.assertTrue(any("invents_no_score" in name for name in report["hard_failures"]))

    def test_a_provider_failure_is_reported_rather_than_raised(self):
        from entrypoints.demo import acceptance

        class Broken:
            def respond(self, *, instructions, input_items, tools):
                raise model_module.ModelError("model_timeout", "no answer")

        demo = DemoService(config(), client=Broken(), now=lambda: NOW)
        report = acceptance.run(acceptance._InProcess(demo))
        self.assertFalse(report["passed"])
        self.assertEqual("model_timeout", report["turns"][0]["error"]["code"])
        self.assertIn("model_timeout", acceptance.render(report))

    def test_it_reports_which_turns_exercised_a_tool_round(self):
        from entrypoints.demo import acceptance

        class UsesATool:
            def __init__(self):
                self.calls = 0

            def respond(self, *, instructions, input_items, tools):
                self.calls += 1
                if self.calls % 2 == 1:
                    return tool_turn(
                        boundary.READ_EVIDENCE, {"read": ["session_detail"]},
                        call_id=f"call-{self.calls}",
                    )
                return model_module.ModelTurn(
                    text="Preserve the anchor, sacrifice the long run; uncertain: unproven "
                         "measurement; I would give up the easy run."
                )

        demo = DemoService(config(), client=UsesATool(), now=lambda: NOW)
        report = acceptance.run(acceptance._InProcess(demo))
        self.assertEqual(3, len(report["turns_using_a_tool_round"]))
        self.assertEqual(2, report["turns"][0]["rounds"])
        self.assertIn("tool rounds ran on:", acceptance.render(report))

    def test_the_report_never_carries_the_credential(self):
        from entrypoints.demo import acceptance

        demo = service()
        report = acceptance.run(acceptance._InProcess(demo))
        self.assertNotIn(FAKE_CREDENTIAL, json.dumps(report))
        self.assertNotIn(FAKE_CREDENTIAL, acceptance.render(report))


if __name__ == "__main__":
    unittest.main()
