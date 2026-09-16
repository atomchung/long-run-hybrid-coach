"""What the public demo endpoint does with a request, and what it refuses to do.

Nothing here reaches a network. The model is a double in every test but one, and that one
patches ``urlopen`` -- so the suite still runs on a bare interpreter with no credential, as
the rest of this repository's does.

The tests are grouped by the thing that would go wrong in public: an unconfigured
deployment answering anyway, one visitor's conversation showing up in another's, a session
that never ends, a caller who does not stop, a model that asks for a write, and a log line
that writes down what somebody typed.
"""

from __future__ import annotations

import datetime as dt
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
from entrypoints.demo.service import DemoRequestError, DemoService


# Short enough to read, long enough to be a real key. Never a real one.
FAKE_CREDENTIAL = "demo-test-credential"
NOW = dt.datetime(2026, 9, 13, 10, 40, tzinfo=dt.timezone.utc)


class FakeModel:
    """A model that says what the test told it to, and records what it was given."""

    def __init__(self, *turns: model_module.ModelTurn) -> None:
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
        return self.turns.pop(0) if len(self.turns) > 1 else self.turns[0]


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


def service(*turns: model_module.ModelTurn, now=None, **overrides: Any) -> DemoService:
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
        self.assertEqual("gpt-6-astra", model_module.MODEL)
        self.assertEqual("gpt-6-astra", captured["body"]["model"])
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
        self.assertEqual("gpt-6-astra", body["model"])
        self.assertEqual("present", body["model_credential"])
        self.assertNotIn(FAKE_CREDENTIAL, json.dumps(body))


if __name__ == "__main__":
    unittest.main()
