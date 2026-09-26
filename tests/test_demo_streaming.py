"""Final demo text streams; unfinished turns never become conversation history.

Provider traffic is fake. A local HTTP socket verifies flush timing and the public frames,
not the pinned model's real behavior; deployed acceptance must establish that separately.
"""

from __future__ import annotations

import datetime as dt
import io
import json
import threading
import unittest
import urllib.error
import urllib.request
from contextlib import contextmanager
from unittest import mock

from entrypoints.demo import acceptance, boundary, model, server
from entrypoints.demo.config import RESPOND_PATH, SITE_ORIGIN
from entrypoints.demo.service import DemoRequestError, DemoService, MAX_TOOL_ROUNDS
from tests.test_demo_service import FAKE_CREDENTIAL, NOW, answer_turn, config, tool_turn


def frame(kind, **payload):
    return (f"event: {kind}\ndata: " + json.dumps({"type": kind, **payload}, ensure_ascii=False) + "\n\n").encode()


def message(text="Hello world", *, phase="final_answer", item_id="m1"):
    return {"id": item_id, "type": "message", "role": "assistant", "phase": phase,
            "content": [{"type": "output_text", "text": text}]}


def added(item, index=0):
    return frame("response.output_item.added", output_index=index, item=item)


def delta(text, index=0, item_id="m1"):
    return frame("response.output_text.delta", output_index=index, item_id=item_id,
                 content_index=0, delta=text)


def completed(output=None, **kwargs):
    return frame("response.completed", response={"status": "completed",
                 "output": output if output is not None else [message()], **kwargs})


def upstream(*events):
    return io.BytesIO(b"".join(events))


def client():
    return model.ResponsesClient(api_key=FAKE_CREDENTIAL,
                                 base_url="https://example.invalid/v1", timeout_seconds=1)


def request(demo, *, on_text=None, on_done=None, session_id="stream-session-1", text="hello"):
    return demo.respond({"session_id": session_id, "message": text}, client_key="test",
                        on_text=on_text, on_done=on_done)


class ProviderStreamingTest(unittest.TestCase):
    def read(self, data, *, allow_tools=True):
        words = []
        result = model._read_stream(upstream(*data), on_text=words.append, allow_tools=allow_tools)
        return result, words

    def test_real_incremental_callback_runs_before_completed_is_read(self):
        words = []
        prefix = added(message()) + delta("Hello ")

        class Paused(io.BytesIO):
            def readline(inner, size=-1):
                if inner.tell() == len(prefix):
                    self.assertEqual(["Hello "], words)
                return super().readline(size)

        with mock.patch("urllib.request.urlopen", return_value=Paused(prefix + delta("world") + completed())) as opened:
            turn = client().respond(instructions="x", input_items=[], on_text=words.append)
        body = json.loads(opened.call_args.args[0].data)
        self.assertTrue(body["stream"])
        self.assertFalse(body["store"])
        self.assertEqual(model.MODEL, body["model"])
        self.assertEqual(["Hello ", "world"], words)
        self.assertEqual("Hello world", turn.text)

    def test_json_client_keeps_nonstreaming_request(self):
        body = {"output": [message()]}
        with mock.patch("urllib.request.urlopen", return_value=io.BytesIO(json.dumps(body).encode())) as opened:
            turn = client().respond(instructions="x", input_items=[])
        self.assertNotIn("stream", json.loads(opened.call_args.args[0].data))
        self.assertEqual("Hello world", turn.text)

    def test_reasoning_commentary_and_tool_arguments_never_reach_callback(self):
        reasoning = {"type": "reasoning", "id": "r1", "encrypted_content": "private-chain"}
        comment = message("private-preamble", phase="commentary", item_id="c1")
        call = {"type": "function_call", "id": "f1", "call_id": "call-1",
                "name": "readDemoEvidence", "arguments": '{"secret":"private-args"}'}
        turn, words = self.read([
            added(reasoning), frame("response.reasoning_text.delta", delta="private-thinking"),
            added(comment, 1), delta("private-preamble", 1, "c1"), added(call, 2),
            frame("response.function_call_arguments.delta", delta="private-args"),
            completed([reasoning, comment, call]),
        ])
        self.assertEqual([], words)
        self.assertEqual("", turn.text)
        self.assertEqual((reasoning, comment, call), turn.output_items)
        self.assertEqual("commentary", model.carry_forward(turn.output_items)[1]["phase"])
        self.assertEqual(1, len(turn.tool_calls))

    def test_unphased_text_is_buffered_while_tools_are_allowed(self):
        item = message(phase=None)
        for phase in (None, "future_phase"):
            with self.subTest(phase=phase):
                item["phase"] = phase
                turn, words = self.read([added(item), delta("Hello world"), completed([item])])
                self.assertEqual([], words)
                self.assertEqual("Hello world", turn.text)
        turn, words = self.read([added(item), delta("Hello world"), completed([item])], allow_tools=False)
        self.assertEqual([], words)  # An unknown phase is never inferred to be final.
        item["phase"] = None
        _, words = self.read([added(item), delta("Hello world"), completed([item])], allow_tools=False)
        self.assertEqual(["Hello world"], words)

    def test_commentary_excluded_from_done_even_with_convenience_aggregate(self):
        comment = message("intermediate", phase="commentary", item_id="c1")
        turn, words = self.read([added(comment), delta("intermediate", item_id="c1"),
                               added(message(), 1), delta("Hello world", 1),
                               completed([comment, message()], output_text="intermediateHello world")])
        self.assertEqual("Hello world", turn.text)
        self.assertEqual(["Hello world"], words)

    def test_unclassified_preamble_cannot_join_a_final_answer_or_exhausted_tool_round(self):
        preamble = message("I will read evidence", phase=None, item_id="p1")
        turn, words = self.read([added(preamble), delta("I will read evidence", item_id="p1"),
                                added(message(), 1), delta("Hello world", 1),
                                completed([preamble, message()])])
        self.assertEqual("Hello world", turn.text)
        self.assertEqual(["Hello world"], words)
        call = {"type": "function_call", "call_id": "call-1", "name": "readDemoEvidence", "arguments": "{}"}
        turn, words = self.read([completed([preamble, call])])
        self.assertEqual("", turn.text)
        self.assertEqual([], words)

    def test_utf8_crlf_comments_and_multiline_json(self):
        item = message("你好")
        data = b": ping\r\n\r\n" + added(item) + delta("你") + delta("好") + completed([item])
        result, words = self.read([data.replace(b"\n", b"\r\n")])
        self.assertEqual("你好", result.text)
        self.assertEqual(["你", "好"], words)
        events = list(model._sse_events(io.BytesIO(b'event: done\ndata: {\ndata: "reply":"yes"}\n\n')))
        self.assertEqual([("done", {"reply": "yes"})], events)

    def test_failure_incomplete_truncated_malformed_and_eof_are_failures(self):
        examples = [
            [added(message()), delta("Hello")],
            [frame("response.failed", response={"error": {"message": "private"}})],
            [frame("response.incomplete", response={"incomplete_details": {"reason": "max_output_tokens"}})],
            [frame("response.incomplete", response={"incomplete_details": {"reason": "content_filter"}})],
            [b"data: not-json\n\n"], [b"data: []\n\n"], [b"data: {}"], [b"data: \xff\n\n"],
            [frame("response.completed", response={"status": "incomplete", "output": [message()]})],
            [frame("response.output_text.delta", output_index=[], delta="private")],
            [added(message()), delta("private", item_id="other")],
            [completed([{**message(), "content": None}])],
            [completed([{**message(), "content": [{"type": "output_text", "text": 42}]}])],
            [added({**message(), "role": "user"}), delta("private")],
            [added(message()), delta("unrelated"), completed()],
            [added(message()), delta("Hello"), added({"type": "function_call"}, 1)],
        ]
        for events in examples:
            with self.subTest(events=events):
                with self.assertRaises(model.ModelError) as caught:
                    self.read(events)
                self.assertIn(caught.exception.code, ("model_unavailable", "model_output_truncated"))
                self.assertNotIn("private", str(caught.exception))

    def test_unbounded_line_event_and_total_bytes_are_refused(self):
        with mock.patch.object(model, "_STREAM_EVENT_LIMIT", 32):
            for raw in (b"x" * 100, b": hi\n" * 10):
                with self.assertRaises(model.ModelError):
                    list(model._sse_events(io.BytesIO(raw)))
        with mock.patch.object(model, "_STREAM_TOTAL_LIMIT", 20):
            with self.assertRaises(model.ModelError):
                list(model._sse_events(io.BytesIO(b": ping\n\n" * 10)))

    def test_stream_refusal_uses_stable_codes_without_provider_message(self):
        for code, expected in [("insufficient_quota", "model_quota_exhausted"),
                               ("rate_limit_exceeded", "model_rate_limited"),
                               ("invalid_api_key", "demo_model_unconfigured"),
                               ("timeout", "model_timeout"), ("other", "model_unavailable")]:
            with self.subTest(code=code), self.assertRaises(model.ModelError) as caught:
                self.read([frame("error", code=code, message="private-user-input")])
            self.assertEqual(expected, caught.exception.code)
            self.assertNotIn("private-user-input", str(caught.exception))


class StreamingModel:
    def __init__(self, *, error=None, chunks=("Hello ", "world")):
        self.error = error
        self.chunks = chunks
        self.calls = 0

    def respond(self, *, on_text=None, **kwargs):
        self.calls += 1
        if on_text:
            for chunk in self.chunks:
                on_text(chunk)
        if self.error:
            raise self.error
        return answer_turn("".join(self.chunks))


class StreamingTransactionTest(unittest.TestCase):
    def test_done_runs_before_commit_with_session_locked_then_commits(self):
        demo = DemoService(config(), client=StreamingModel(), now=lambda: NOW)
        session = demo.sessions.get_or_create("stream-session-1", now=NOW)
        calls = []

        def done(body):
            self.assertTrue(session.lock.locked())
            self.assertEqual([], session.history)
            self.assertEqual(1, body["turn"])
            calls.append(body)

        reply = request(demo, on_text=lambda _: None, on_done=done)
        self.assertEqual([reply.body], calls)
        self.assertTrue(session.history)
        self.assertFalse(session.lock.locked())

    def test_disconnect_during_delta_or_done_refunds_unlocks_and_preserves_history(self):
        for disconnect_at in ("delta", "done"):
            with self.subTest(disconnect_at=disconnect_at):
                demo = DemoService(config(), client=StreamingModel(), now=lambda: NOW)
                request(demo)
                session = demo.sessions.get_or_create("stream-session-1", now=NOW)
                before = list(session.history)

                def disconnected(_):
                    raise BrokenPipeError("gone")

                with self.assertRaises(BrokenPipeError):
                    request(demo, on_text=disconnected if disconnect_at == "delta" else lambda _: None,
                            on_done=disconnected if disconnect_at == "done" else None)
                self.assertEqual(before, session.history)
                self.assertEqual(1, session.turns)
                self.assertFalse(session.lock.locked())
                self.assertEqual(2, request(demo).body["turn"])

    def test_tool_preview_is_staged_and_rolled_back_on_failure(self):
        class PreviewThenFail:
            def respond(inner, *, on_text=None, **kwargs):
                if len(kwargs["input_items"]) == 1:
                    return tool_turn("previewDemoChange", {})
                if on_text:
                    on_text("Not complete")
                raise model.ModelError("model_timeout", "the model did not answer in time")

        demo = DemoService(config(), client=PreviewThenFail(), now=lambda: NOW)
        session = demo.sessions.get_or_create("stream-session-1", now=NOW)
        session.previews = [{"previous": True}]
        with mock.patch.object(boundary, "dispatch", return_value={"status": "preview_only", "preview": {"new": True}}):
            with self.assertRaises(DemoRequestError):
                request(demo, on_text=lambda _: None)
        self.assertEqual([{"previous": True}], session.previews)
        self.assertEqual([], session.history)
        self.assertEqual(0, session.turns)

    def test_live_lock_prevents_expiry_replacement_and_concurrent_turn(self):
        now = [NOW]
        demo = DemoService(config(session_ttl_seconds=1), client=StreamingModel(), now=lambda: now[0])
        session = demo.sessions.get_or_create("stream-session-1", now=NOW)

        def during_delta(_):
            now[0] = NOW + dt.timedelta(seconds=5)
            self.assertEqual(0, demo.purge_expired())
            self.assertIs(session, demo.sessions.get_or_create("stream-session-1", now=now[0]))
            with self.assertRaises(DemoRequestError) as caught:
                request(demo)
            self.assertEqual("session_busy", caught.exception.code)

        request(demo, on_text=during_delta)
        self.assertEqual(now[0], session.last_seen_at)
        self.assertEqual(0, demo.purge_expired())

    def test_multi_round_usage_and_reasoning_phase_continuity_survive_streaming(self):
        reasoning = {"type": "reasoning", "id": "r1", "encrypted_content": "opaque"}
        comment = message("reading evidence", phase="commentary", item_id="c1")
        call = {"type": "function_call", "call_id": "call-1", "name": "readDemoEvidence", "arguments": "{}"}
        first = completed([reasoning, comment, call], usage={"input_tokens": 10, "output_tokens": 5,
                          "input_tokens_details": {"cached_tokens": 0}, "output_tokens_details": {"reasoning_tokens": 3}})
        second = added(message()) + delta("Hello world") + completed(usage={"input_tokens": 20, "output_tokens": 6,
                           "input_tokens_details": {"cached_tokens": 4}, "output_tokens_details": {"reasoning_tokens": 2}})
        demo = DemoService(config(), client=client(), now=lambda: NOW)
        sent = []

        def open_fake(req, **kwargs):
            sent.append(json.loads(req.data))
            return io.BytesIO(first if len(sent) == 1 else second)

        with mock.patch("urllib.request.urlopen", side_effect=open_fake):
            reply = request(demo, on_text=lambda _: None)
        self.assertEqual(2, reply.log["rounds"])
        self.assertEqual((30, 4, 11, 5), tuple(reply.log[k] for k in (
            "input_tokens", "cached_tokens", "output_tokens", "reasoning_tokens")))
        self.assertEqual([reasoning, comment, call], sent[1]["input"][1:4])
        self.assertEqual("function_call_output", sent[1]["input"][4]["type"])

    def test_streaming_keeps_round_tool_and_request_limits(self):
        calls = []

        class CallsForever:
            def respond(inner, *, allow_tools, on_text=None, **kwargs):
                calls.append(allow_tools)
                output = [{"type": "function_call", "call_id": f"{len(calls)}-{i}",
                           "name": "readDemoEvidence", "arguments": json.dumps({"group": str(i)})}
                          for i in range(12)]
                return model.parse_response({"output": output})

        demo = DemoService(config(max_turns_per_session=1, requests_per_minute_per_client=2),
                           client=CallsForever(), now=lambda: NOW)
        with mock.patch.object(boundary, "dispatch", return_value={"ok": True}) as dispatched:
            request(demo, on_text=lambda _: None)
        self.assertEqual([True, True, False], calls)
        self.assertEqual(MAX_TOOL_ROUNDS, len(calls))
        self.assertLessEqual(dispatched.call_count, 8)
        with self.assertRaises(DemoRequestError) as caught:
            request(demo, on_text=lambda _: None)
        self.assertEqual("turn_limit_reached", caught.exception.code)
        with self.assertRaises(DemoRequestError) as caught:
            request(demo, session_id="stream-session-2", on_text=lambda _: None)
        self.assertEqual("rate_limited", caught.exception.code)


@contextmanager
def listening(client_double, **overrides):
    demo = DemoService(config(allowed_origins=(SITE_ORIGIN,), **overrides), client=client_double, now=lambda: NOW)
    httpd = server.DemoServer(("127.0.0.1", 0), demo)
    thread = threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}", demo
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(5)


def post(base, *, accept="text/event-stream, application/json", origin=SITE_ORIGIN, body=None):
    req = urllib.request.Request(base + RESPOND_PATH,
        data=json.dumps(body or {"session_id": "stream-http-1", "message": "hello", "locale": "zh-Hant"}).encode(),
        headers={"Content-Type": "application/json", "Accept": accept, "Origin": origin}, method="POST")
    return urllib.request.urlopen(req, timeout=5)


class StreamingHttpTest(unittest.TestCase):
    def test_local_socket_receives_delta_before_model_finishes_and_done_once(self):
        release = threading.Event()

        class Waiting:
            def respond(inner, *, on_text, **kwargs):
                on_text("你好")
                if not release.wait(5):
                    raise RuntimeError("test consumer did not receive the delta")
                return answer_turn("你好世界")

        with listening(Waiting()) as (base, demo):
            try:
                with post(base) as response:
                    self.assertEqual("text/event-stream", response.headers.get_content_type())
                    self.assertEqual(SITE_ORIGIN, response.headers["Access-Control-Allow-Origin"])
                    self.assertEqual("no", response.headers["X-Accel-Buffering"])
                    self.assertEqual("no-store", response.headers["Cache-Control"])
                    events = model._sse_events(response)
                    self.assertEqual(("delta", {"text": "你好"}), next(events))
                    release.set()
                    self.assertEqual([("done", {"reply": "你好世界", "turn": 1})], list(events))
            finally:
                release.set()

    def test_done_only_fallback_and_json_negotiation(self):
        class FinalOnly:
            def respond(inner, **kwargs):
                return answer_turn("Done only")

        with listening(FinalOnly()) as (base, _):
            with post(base) as response:
                self.assertEqual([("done", {"reply": "Done only", "turn": 1})], list(model._sse_events(response)))
            for accept in ("application/json", "*/*", "text/event-stream;q=0, application/json", "application/json, text/event-stream;q=0.5"):
                with post(base, accept=accept) as response:
                    self.assertEqual("application/json", response.headers.get_content_type())
                    self.assertEqual("Done only", json.load(response)["reply"])

    def test_errors_before_stream_are_json_after_delta_are_sanitized_sse(self):
        for chunks in ((), ("partial",)):
            failing = StreamingModel(error=model.ModelError("model_timeout", "the model did not answer in time"), chunks=chunks)
            with listening(failing) as (base, demo):
                if chunks:
                    with post(base) as response:
                        events = list(model._sse_events(response))
                        self.assertEqual(["delta", "error"], [e[0] for e in events])
                        self.assertEqual("model_timeout", events[-1][1]["error"]["code"])
                else:
                    with self.assertRaises(urllib.error.HTTPError) as caught:
                        post(base)
                    self.assertEqual(504, caught.exception.code)
                    self.assertEqual("model_timeout", json.load(caught.exception)["error"]["code"])
                session = demo.sessions.get_or_create("stream-http-1", now=NOW)
                self.assertEqual(0, session.turns)
                self.assertEqual([], session.history)
                self.assertFalse(session.lock.locked())

    def test_post_delta_error_preserves_retry_after_and_never_sends_done(self):
        class BusyAfterText:
            def respond(inner, *, on_text, **kwargs):
                on_text("partial")
                raise DemoRequestError(429, "rate_limited", "busy", retry_after=23)

        with listening(BusyAfterText()) as (base, _):
            with post(base) as response:
                events = list(model._sse_events(response))
            self.assertEqual(["delta", "error"], [event for event, _ in events])
            self.assertEqual({"error": {"code": "rate_limited", "message": "busy"}, "retry_after": 23}, events[-1][1])

    def test_sse_cannot_skip_origin_body_or_rate_limits(self):
        with listening(StreamingModel(), requests_per_minute_per_client=1) as (base, _):
            for kwargs, status in (({"origin": "https://bad.example"}, 403),
                                   ({"body": {"session_id": "short", "message": "hello"}}, 400),
                                   ({}, 429)):
                with self.subTest(kwargs=kwargs), self.assertRaises(urllib.error.HTTPError) as caught:
                    post(base, **kwargs)
                self.assertEqual(status, caught.exception.code)
                self.assertIn("error", json.load(caught.exception))

    def test_write_failure_at_done_never_commits_and_releases_session(self):
        class DoneOnly:
            def respond(inner, **kwargs):
                return answer_turn("Done only")

        with listening(DoneOnly()) as (base, demo):

            def broken_headers(handler):
                # Simulate a peer reset when beginning the done-only stream.
                raise BrokenPipeError("synthetic disconnect")

            with mock.patch.object(server.DemoHandler, "end_headers", broken_headers):
                with self.assertRaises(Exception):
                    post(base)
            session = demo.sessions.get_or_create("stream-http-1", now=NOW)
            self.assertEqual(0, session.turns)
            self.assertEqual([], session.history)
            self.assertFalse(session.lock.locked())

    def test_deployed_acceptance_reader_reports_deltas_and_rejects_truncation(self):
        with listening(StreamingModel()) as (base, _):
            outcome = acceptance._OverHttp(base, timeout=5, stream=True).ask("acceptance-1", "hello")
            self.assertTrue(outcome["ok"])
            self.assertEqual(2, outcome["stream"]["delta_count"])
            self.assertIsInstance(outcome["stream"]["first_text_ms"], int)
            self.assertGreaterEqual(outcome["stream"]["first_text_to_done_ms"], 0)
        raw = io.BytesIO(b'event: delta\ndata: {"text":"partial"}\n\n')
        raw.status = 200
        self.assertEqual("stream_incomplete", acceptance._OverHttp._read_stream(raw, started=0)["code"])


if __name__ == "__main__":
    unittest.main()
