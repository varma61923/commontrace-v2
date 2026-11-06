"""Deferred egress owns its iterator even when limits or client writes abort it."""

import contextvars
import io

import pytest

from unimem import gateway


class TrackingSource:
    def __init__(self, on_close):
        self.on_close = on_close
        self.reads = 0

    def __iter__(self):
        return self

    def __next__(self):
        self.reads += 1
        return b"one"

    def close(self):
        self.on_close()


def test_unstarted_stream_close_releases_source_once_in_captured_context():
    identity = contextvars.ContextVar("unstarted-stream-cleanup", default="outside")
    closed = []
    source = TrackingSource(lambda: closed.append(identity.get()))
    token = identity.set("authorized")
    try:
        response = gateway.Response.streaming(200, source, content_type="text/plain")
    finally:
        identity.reset(token)
    response.stream.close()
    response.stream.close()
    assert closed == ["authorized"]
    with pytest.raises(StopIteration):
        next(response.stream)
    assert source.reads == 0
    assert identity.get() == "outside"


@pytest.mark.parametrize("stage", ["send_response", "send_header", "end_headers"])
def test_http_header_failure_closes_source_before_any_consumption(tmp_path, stage):
    closed = []
    source = TrackingSource(lambda: closed.append(True))

    def disconnected(*_args):
        raise BrokenPipeError("client disconnected during headers")

    app = gateway.Gateway(str(tmp_path))
    server = gateway.make_http_server(app, "127.0.0.1", 0)
    try:
        handler = object.__new__(server.RequestHandlerClass)
        handler.send_response = lambda _status: None
        handler.send_header = lambda _key, _value: None
        handler.end_headers = lambda: None
        setattr(handler, stage, disconnected)
        handler.wfile = io.BytesIO()
        handler.close_connection = False
        response = gateway.Response.streaming(200, source, content_type="text/plain")
        handler._send(response)
        response.stream.close()
        assert handler.close_connection
        assert source.reads == 0
        assert closed == [True]
    finally:
        server.server_close()


@pytest.mark.parametrize("invalid", ["not bytes", b"x" * (256 * 1024 + 1)])
def test_stream_validation_closes_source_in_its_captured_context(invalid):
    identity = contextvars.ContextVar("stream-cleanup-identity", default="outside")
    closed = []

    def source():
        try:
            yield invalid
        finally:
            closed.append(identity.get())

    token = identity.set("authorized")
    try:
        response = gateway.Response.streaming(200, source(), content_type="text/plain")
    finally:
        identity.reset(token)
    with pytest.raises(ValueError, match="stream"):
        next(response.stream)
    assert closed == ["authorized"]
    assert identity.get() == "outside"


def test_stream_cancellation_closes_the_source_without_waiting_for_gc():
    closed = []

    def source():
        try:
            yield b"one"
            yield b"two"
        finally:
            closed.append(True)

    response = gateway.Response.streaming(200, source(), content_type="text/plain")
    assert next(response.stream) == b"one"
    response.stream.close()
    assert closed == [True]


def test_http_write_failure_closes_response_stream(tmp_path):
    closed = []

    def source():
        try:
            yield b"one"
            yield b"two"
        finally:
            closed.append(True)

    class Disconnected:
        def write(self, _chunk):
            raise BrokenPipeError("client disconnected")

    app = gateway.Gateway(str(tmp_path))
    server = gateway.make_http_server(app, "127.0.0.1", 0)
    try:
        handler = object.__new__(server.RequestHandlerClass)
        handler.send_response = lambda _status: None
        handler.send_header = lambda _key, _value: None
        handler.end_headers = lambda: None
        handler.wfile = Disconnected()
        handler.close_connection = False
        response = gateway.Response.streaming(200, source(), content_type="text/plain")
        handler._send(response)
        assert handler.close_connection
        assert closed == [True]
    finally:
        server.server_close()
