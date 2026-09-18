"""The HTTP surface: one route that answers, one that reports health, and CORS.

Deliberately small. It parses a request, decides who is asking for rate-limiting purposes,
hands the rest to ``DemoService``, and writes the answer. Nothing coaching-related happens
here, and nothing here can be reached without going through the service's own limits.

The endpoint is public and anonymous, so the shape of what it accepts is the first defence:
JSON only and never a multipart body, so there is no upload path; a declared length that is
checked before the body is read, so a large body is refused rather than buffered; a closed
set of three fields -- a session id, a message and an optional language tag, each checked
against its own shape -- so nothing else can be smuggled in; and no URL of any kind taken
from a caller, so there is no arbitrary MCP endpoint, webhook or callback to point this at.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from . import fixture
from .config import DemoConfig, from_environment
from .service import (
    DemoReply,
    DemoRequestError,
    DemoService,
    HEALTH_PATH,
    RESPOND_PATH,
    error_body,
)


LOGGER = logging.getLogger("entrypoints.demo")


class DemoHandler(BaseHTTPRequestHandler):
    server_version = "long-run-hybrid-coach-demo"
    sys_version = ""

    # A connection that opens and then sends nothing holds a worker thread for as long as
    # it likes, and never reaches the rate limiter -- which counts requests, not sockets.
    # Fifteen seconds is longer than any honest client needs to send at most 8 KB.
    timeout = 15

    # --------------------------------------------------------------------------- plumbing
    @property
    def _service(self) -> DemoService:
        return self.server.service  # type: ignore[attr-defined]

    @property
    def _config(self) -> DemoConfig:
        return self._service.config

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - base signature
        """Drop the default access log.

        It prints the raw request line. This service never puts anything in a URL, so that
        line would be harmless today and one query parameter away from not being. What is
        logged instead is written explicitly in ``_finish``.
        """

    def _client_key(self) -> str:
        """Who to count this request against.

        Behind Railway the socket peer is the platform's proxy, so every visitor would
        share one bucket and the per-client limit would mean nothing. ``forwarded`` reads
        ``X-Forwarded-For`` instead -- the **right-most** entry, because each proxy appends
        the address it received the connection from: the last entry is the one the platform
        in front wrote, and everything to the left of it is whatever the caller sent. Read
        the left-most and one host rotates the header for a fresh allowance per request,
        which is the per-client limit meaning nothing and the global ceiling being the only
        brake left.
        """
        if self._config.client_ip_source == "forwarded":
            forwarded = self.headers.get("X-Forwarded-For")
            if forwarded:
                last = forwarded.split(",")[-1].strip()
                if last:
                    return last[:64]
        return str(self.client_address[0])[:64]

    def _origin(self) -> str | None:
        origin = self.headers.get("Origin")
        return origin.strip() if origin else None

    def _cors_headers(self, origin: str | None) -> list[tuple[str, str]]:
        headers = [("Vary", "Origin")]
        if origin and origin in self._config.allowed_origins:
            headers.extend(
                [
                    ("Access-Control-Allow-Origin", origin),
                    ("Access-Control-Allow-Methods", "POST, OPTIONS"),
                    ("Access-Control-Allow-Headers", "Content-Type"),
                    ("Access-Control-Max-Age", "600"),
                ]
            )
        return headers

    def _send(
        self,
        status: int,
        body: dict[str, Any],
        *,
        origin: str | None,
        retry_after: int | None = None,
    ) -> None:
        raw = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        if retry_after is not None:
            self.send_header("Retry-After", str(retry_after))
        for name, value in self._cors_headers(origin):
            self.send_header(name, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(raw)

    def _finish(self, status: int, entry: dict[str, Any]) -> None:
        """The only access log this service writes.

        Method, path, status and the service's own summary. Never the visitor's sentence,
        never the athlete payload, never a header, never a credential, and never the raw
        session id -- ``session`` below is a hash prefix, which joins a trace together
        without writing down the value that would resume the conversation.
        """
        LOGGER.info(
            json.dumps(
                {"method": self.command, "path": self.path.split("?")[0][:200], "status": status,
                 **entry},
                sort_keys=True,
            )
        )

    # ----------------------------------------------------------------------------- routes
    def do_OPTIONS(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler naming
        origin = self._origin()
        path = self.path.split("?")[0]
        if path != RESPOND_PATH:
            self._send(HTTPStatus.NOT_FOUND, _error("not_found", "no such route"), origin=origin)
            self._finish(404, {"event": "preflight"})
            return
        if origin is not None and origin not in self._config.allowed_origins:
            self._send(
                HTTPStatus.FORBIDDEN,
                _error("origin_not_allowed", "this origin may not call the demo"),
                origin=None,
            )
            self._finish(403, {"event": "preflight"})
            return
        self.send_response(HTTPStatus.NO_CONTENT)
        self.send_header("Content-Length", "0")
        for name, value in self._cors_headers(origin):
            self.send_header(name, value)
        self.end_headers()
        self._finish(204, {"event": "preflight"})

    def do_GET(self) -> None:  # noqa: N802
        origin = self._origin()
        path = self.path.split("?")[0]
        if path != HEALTH_PATH:
            self._send(HTTPStatus.NOT_FOUND, _error("not_found", "no such route"), origin=origin)
            self._finish(404, {"event": "request"})
            return
        reply = self._service.health()
        self._send(reply.status, reply.body, origin=origin)
        self._finish(reply.status, reply.log)

    def do_HEAD(self) -> None:  # noqa: N802
        self.do_GET()

    def do_POST(self) -> None:  # noqa: N802
        origin = self._origin()
        path = self.path.split("?")[0]
        if path != RESPOND_PATH:
            self._send(HTTPStatus.NOT_FOUND, _error("not_found", "no such route"), origin=origin)
            self._finish(404, {"event": "request"})
            return
        if origin is not None and origin not in self._config.allowed_origins:
            self._send(
                HTTPStatus.FORBIDDEN,
                _error("origin_not_allowed", "this origin may not call the demo"),
                origin=None,
            )
            self._finish(403, {"event": "request"})
            return
        try:
            payload = self._json_body()
            reply = self._service.respond(payload, client_key=self._client_key())
        except DemoRequestError as error:
            self._send(
                error.status, error_body(error), origin=origin, retry_after=error.retry_after
            )
            self._finish(error.status, {"event": "request", "code": error.code})
            return
        except Exception:  # noqa: BLE001 - a demo turn must not return a stack trace
            LOGGER.exception("demo turn failed")
            self._send(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                _error("internal_error", "the demo could not answer this turn"),
                origin=origin,
            )
            self._finish(500, {"event": "request", "code": "internal_error"})
            return
        self._send(reply.status, reply.body, origin=origin, retry_after=reply.retry_after)
        self._finish(reply.status, reply.log)

    # ------------------------------------------------------------------------------- body
    def _json_body(self) -> Any:
        content_type = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if content_type and content_type != "application/json":
            # Including every multipart type, which is the only way a file would arrive.
            raise DemoRequestError(
                HTTPStatus.UNSUPPORTED_MEDIA_TYPE,
                "unsupported_media_type",
                "the demo takes application/json",
            )
        raw_length = self.headers.get("Content-Length")
        try:
            length = int(raw_length)
        except (TypeError, ValueError):
            raise DemoRequestError(
                HTTPStatus.LENGTH_REQUIRED, "length_required", "a Content-Length is required"
            ) from None
        if length < 0 or length > self._config.max_body_bytes:
            raise DemoRequestError(
                HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                "payload_too_large",
                f"the body must be at most {self._config.max_body_bytes} bytes",
            )
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise DemoRequestError(
                HTTPStatus.BAD_REQUEST, "invalid_request", "the body must be valid JSON"
            ) from None


def _error(code: str, message: str) -> dict[str, Any]:
    return {"error": {"code": code, "message": message}}


class DemoServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int], service: DemoService) -> None:
        super().__init__(address, DemoHandler)
        self.service = service


class StartupError(RuntimeError):
    """A deployment that should not start rather than answer wrongly."""


def build_service(config: DemoConfig | None = None) -> DemoService:
    """The service, refusing to exist when the deployment cannot honestly answer.

    Both refusals are startup-time on purpose. A missing credential is a deployment that
    would answer every request with a 503, and a fixture that no longer validates is a
    deployment answering from a shape the product's contracts no longer have -- neither is
    worth discovering one visitor at a time.
    """
    config = config or from_environment()
    report = fixture.validate()
    if report["errors"]:
        raise StartupError(
            "the demo fixture does not validate against the product contracts: "
            + "; ".join(report["errors"])
        )
    if not config.has_api_key:
        raise StartupError(
            "OPENAI_API_KEY is not set. The demo calls the Responses API on a pinned model "
            "and has no other way to answer; it will not start without a credential."
        )
    if config.unknown_env:
        LOGGER.warning(
            json.dumps({"event": "unknown_config", "names": list(config.unknown_env)})
        )
    for line in report["warnings"]:
        LOGGER.info(json.dumps({"event": "fixture_warning", "detail": line}))
    return DemoService(config)


def serve(config: DemoConfig | None = None) -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    service = build_service(config)
    server = DemoServer((service.config.host, service.config.port), service)
    LOGGER.info(
        json.dumps(
            {
                "event": "listening",
                "port": service.config.port,
                "origins": list(service.config.allowed_origins),
                "started_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            },
            sort_keys=True,
        )
    )
    try:
        server.serve_forever()
    finally:
        server.server_close()
