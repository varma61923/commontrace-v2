"""Network body reads must have a total deadline, not just a per-read timeout."""

from __future__ import annotations

import io
import socket
import threading
import time
import urllib.request

import pytest

from unimem.retry import read_within


def test_reads_everything_up_to_one_byte_past_the_limit():
    assert read_within(io.BytesIO(b"abc"), 10, 5) == b"abc"
    assert read_within(io.BytesIO(b"x" * 100), 10, 5) == b"x" * 11
    assert read_within(io.BytesIO(b""), 10, 5) == b""


def test_a_body_exactly_at_the_limit_is_accepted():
    assert read_within(io.BytesIO(b"x" * 10), 10, 5) == b"x" * 10


def test_a_plain_reader_without_read1_is_supported():
    class Plain:
        def __init__(self, data):
            self._data = io.BytesIO(data)

        def read(self, n):
            return self._data.read(n)

    assert read_within(Plain(b"hello"), 10, 5) == b"hello"


@pytest.mark.parametrize("chunk", [b"", b"complete"])
@pytest.mark.parametrize("buffered", [True, False])
def test_a_final_read_cannot_succeed_after_the_deadline(monkeypatch, chunk, buffered):
    from unimem import retry

    clock = [0.0]

    class LateResponse:
        def read(self, n):
            clock[0] = 2.0
            return chunk

    response = LateResponse()
    if buffered:
        response.read1 = response.read
    monkeypatch.setattr(retry.time, "monotonic", lambda: clock[0])
    with pytest.raises(TimeoutError, match="within 1s"):
        read_within(response, 100, 1)


@pytest.fixture
def trickle_server():
    """An HTTP server that sends headers, then one body byte every 0.2 s."""
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    stop = threading.Event()

    def serve():
        conn, _ = listener.accept()
        with conn:
            conn.recv(65536)
            conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 1000\r\n\r\n")
            while not stop.is_set():
                try:
                    conn.sendall(b"x")
                except OSError:
                    return
                time.sleep(0.2)

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{listener.getsockname()[1]}/"
    stop.set()
    listener.close()
    thread.join(timeout=5)


def test_a_trickling_server_cannot_outlive_the_total_deadline(trickle_server):
    # Regression: each 0.2 s byte reset urllib's 1 s per-read timeout, so the
    # read ran for as long as the server kept trickling.
    started = time.monotonic()
    with urllib.request.urlopen(trickle_server, timeout=1) as response:  # nosec B310 - loopback test
        with pytest.raises(TimeoutError, match="within 1s"):
            read_within(response, 8 * 1024 * 1024, 1)
    assert time.monotonic() - started < 4
