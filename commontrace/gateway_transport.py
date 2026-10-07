"""HTTP and newline-delimited JSON transports for the gateway application.

Transport framing, TLS, socket timeouts and response serialization live here;
authentication and store operations remain in :mod:`commontrace.gateway`.
The gateway's public factory functions are compatibility facades.
"""
from __future__ import annotations

import json
import logging
import math
import re
import ssl
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import TYPE_CHECKING, Any, TextIO
from urllib.parse import urlencode, urlsplit

if TYPE_CHECKING:
    from commontrace.gateway import Gateway, Response

logger = logging.getLogger("commontrace.gateway")

__all__ = ["make_http_server", "serve_stdio"]

def make_http_server(gateway: Gateway, host: str, port: int, *, tls: tuple[str, str] | None = None,
                     request_timeout: float = 10.0, max_connections: int = 128) -> ThreadingHTTPServer:
    """Create an HTTP/TLS server with bounded bodies and active connections.

    Excess connections close immediately, without allocating waiting workers.
    Persistent connections retain their slot until completion or idle timeout.
    """
    from commontrace.gateway import MAX_BODY_BYTES, _json

    if not isinstance(max_connections, int) or isinstance(max_connections, bool) or max_connections < 1:
        raise ValueError("max_connections must be a positive integer")
    if not math.isfinite(request_timeout) or request_timeout <= 0:
        raise ValueError("request_timeout must be finite and positive")

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        timeout = request_timeout
        # Headers and small JSON bodies are separate writes. On persistent
        # connections Nagle can hold the body for the peer's delayed ACK.
        disable_nagle_algorithm = True

        def log_message(self, format: str, *args: Any) -> None:
            logger.debug("Gateway HTTP: %s", format % args)

        def handle(self):
            if isinstance(self.connection, ssl.SSLSocket):
                try:
                    self.connection.do_handshake()
                except (ssl.SSLError, OSError):
                    self.close_connection = True
                    return
            super().handle()

        def parse_request(self) -> bool:
            if not super().parse_request():
                return False
            # A persistent stream must have one unambiguous framing rule.
            # Reject unsupported encodings even when they omit 'chunked'.
            if self.headers.get_all("Transfer-Encoding"):
                self._fail(411, "length_required", "Transfer-Encoding is not supported")
                return False
            lengths = self.headers.get_all("Content-Length", [])
            if len(lengths) > 1 or (lengths and not re.fullmatch(r"[0-9]+", lengths[0])):
                self._fail(400, "bad_length", "send one nonnegative Content-Length")
                return False
            if lengths:
                significant = lengths[0].lstrip("0") or "0"
                if len(significant) > len(str(MAX_BODY_BYTES)) or int(significant) > MAX_BODY_BYTES:
                    self._fail(413, "too_large", f"body is larger than {MAX_BODY_BYTES} bytes")
                    return False
                # Normalize zero padding before integer conversion in do_POST.
                self.headers.replace_header("Content-Length", significant)
                lengths = [significant]
            if self.command == "GET" and lengths and int(lengths[0]) != 0:
                self._fail(400, "unexpected_body", "GET requests do not accept a body")
                return False
            return True

        def _send(self, response: Response) -> None:
            self.send_response(response.status)
            self.send_header("Content-Type", response.content_type)
            self.send_header("Content-Length", str(len(response.body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            for key, value in response.headers.items():
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(response.body)

        def _fail(self, status: int, code: str, message: str) -> None:
            self._send(_json(status, {"error": {"code": code, "message": message}}))
            self.close_connection = True

        def do_GET(self):  # noqa: N802
            self._send(gateway.handle("GET", self.path, dict(self.headers.items())))

        def do_POST(self):  # noqa: N802
            if "chunked" in (self.headers.get("Transfer-Encoding") or "").lower():
                return self._fail(411, "length_required", "send a Content-Length, not chunked encoding")
            try:
                length = int(self.headers.get("Content-Length", ""))
            except ValueError:
                return self._fail(411, "length_required", "Content-Length is required")
            if length < 0 or length > MAX_BODY_BYTES:
                return self._fail(413, "too_large", f"body is larger than {MAX_BODY_BYTES} bytes")
            ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            if ctype != "application/json":
                return self._fail(415, "unsupported_media_type", "Content-Type must be application/json")
            origin, host_header = self.headers.get("Origin"), self.headers.get("Host", "")
            if origin and urlsplit(origin).netloc != host_header:
                return self._fail(403, "bad_origin", "cross-origin requests are not accepted")
            body = self.rfile.read(length)
            if len(body) != length:
                return self._fail(400, "incomplete_body", "body is shorter than Content-Length")
            self._send(gateway.handle("POST", self.path, dict(self.headers.items()), body))

        do_PUT = do_DELETE = do_PATCH = lambda self: self._fail(  # noqa: E731
            405, "method_not_allowed", "only GET and POST are used")

    class Server(ThreadingHTTPServer):
        daemon_threads = True
        allow_reuse_address = True
        request_queue_size = 64

        def __init__(self, *args, **kwargs):
            self.connection_slots = threading.BoundedSemaphore(max_connections)
            super().__init__(*args, **kwargs)

        def process_request(self, request, client_address):
            # Never block the accept loop or create a waiting worker. Closing
            # excess TLS sockets also avoids sending plaintext before a handshake.
            if not self.connection_slots.acquire(blocking=False):
                self.shutdown_request(request)
                return
            try:
                super().process_request(request, client_address)
            except BaseException:
                self.connection_slots.release()
                raise

        def process_request_thread(self, request, client_address):
            try:
                super().process_request_thread(request, client_address)
            finally:
                self.connection_slots.release()

    server = Server((host, port), Handler)
    try:
        if tls:
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.minimum_version = ssl.TLSVersion.TLSv1_2
            context.load_cert_chain(*tls)
            server.socket = context.wrap_socket(server.socket, server_side=True, do_handshake_on_connect=False)
    except BaseException:
        server.server_close()
        raise
    return server


def serve_stdio(gateway: Gateway, stdin: TextIO, stdout: TextIO) -> int:
    """One JSON object per line in, one per line out, until EOF."""
    shorthand = {"recall": ("POST", "/v1/recall"), "outcome": ("POST", "/v1/outcome"),
                 "status": ("GET", "/v1/status"), "memories": ("GET", "/v1/memories"),
                 "occasions": ("GET", "/v1/occasions"), "agents": ("GET", "/v1/agents"),
                 "health": ("GET", "/v1/health"),
                 "remember": ("POST", "/v1/conversation/add"), "converse": ("POST", "/v1/conversation/recall"),
                 "conversation_add": ("POST", "/v1/conversation/add"),
                 "conversation_recall": ("POST", "/v1/conversation/recall"),
                 "lessons": ("GET", "/v1/lessons"), "lesson": ("GET", "/v1/lesson"),
                 "lesson_edit": ("POST", "/v1/lesson/edit"), "edit": ("POST", "/v1/lesson/edit"),
                 "lesson_approve": ("POST", "/v1/lesson/approve"), "approve": ("POST", "/v1/lesson/approve"),
                 "lesson_reject": ("POST", "/v1/lesson/reject"), "reject": ("POST", "/v1/lesson/reject"),
                 "proof": ("GET", "/v1/status"), "proof_status": ("GET", "/v1/status")}

    def with_query(base: str, params: dict) -> str:
        clean = {k: v for k, v in params.items() if v is not None}
        if not clean:
            return base
        qs = urlencode(clean, doseq=True)
        return base + ("&" if "?" in base else "?") + qs if qs else base

    for line in stdin:
        line = line.strip()
        if not line:
            continue
        request_id = None
        try:
            req = json.loads(line)
            if not isinstance(req, dict):
                raise ValueError("a request must be a JSON object")
            request_id = req.get("id")
            if "op" in req:
                if not isinstance(req["op"], str):
                    raise ValueError("op must be a string")
                if req["op"] not in shorthand:
                    raise ValueError(f"unknown op {req['op']!r}")
                method, base = shorthand[req["op"]]
                params = {k: v for k, v in req.items() if k not in ("op", "id")}
                if method == "GET":
                    path, body = with_query(base, params), {}
                else:
                    path, body = base, params
            else:
                method, path = req.get("method", "POST"), req["path"]
                if not isinstance(method, str) or not method:
                    raise ValueError("method must be a nonempty string")
                if not isinstance(path, str) or not path:
                    raise ValueError("path must be a nonempty string")
                body = req.get("body")
                if body is None:
                    body = {}
                elif not isinstance(body, dict):
                    raise ValueError("body must be a JSON object")
                if method == "GET" and body:
                    path, body = with_query(path, body), {}
            response = gateway.handle(
                method, path, body=json.dumps(body).encode("utf-8") if method == "POST" else None,
                trusted=True)
            reply = {"id": request_id, "status": response.status, "body": json.loads(response.body)}
        except (ValueError, KeyError, RecursionError) as exc:
            reply = {"id": request_id, "status": 400,
                     "body": {"error": {"code": "bad_request", "message": str(exc)}}}
        stdout.write(json.dumps(reply, separators=(",", ":")) + "\n")
        stdout.flush()
    return 0
