"""Unauthenticated slow input cannot retain a bounded gateway worker forever."""

from __future__ import annotations

import http.client
import socket
import threading
import time
from contextlib import closing

import pytest

from tests.test_gateway import TOKEN
from unimem.gateway import Gateway, make_http_server


@pytest.mark.parametrize("stage", ["headers", "body"])
def test_trickling_input_expires_and_releases_the_connection_slot(tmp_path, stage):
    server = make_http_server(
        Gateway(str(tmp_path), token=TOKEN), "127.0.0.1", 0, request_timeout=0.3, max_connections=1
    )
    serving = threading.Thread(target=server.serve_forever, daemon=True)
    serving.start()
    stop = threading.Event()
    client = socket.create_connection(server.server_address, timeout=3)
    if stage == "headers":
        initial = b"POST /v1/recall HTTP/1.1\r\nHost: localhost\r\nX-Fill: "
    else:
        initial = b"POST /v1/recall HTTP/1.1\r\nHost: localhost\r\nContent-Type: application/json\r\nContent-Length: 10000\r\n\r\n"
    client.sendall(initial)

    def trickle():
        while not stop.wait(0.05):
            try:
                client.sendall(b"x")
            except OSError:
                return

    sending = threading.Thread(target=trickle, daemon=True)
    sending.start()
    try:
        try:
            assert client.recv(1) == b""
        except ConnectionResetError:
            pass
        # Wait for worker completion through the server's actual admission gate.
        assert server.connection_slots.acquire(timeout=3)
        server.connection_slots.release()
        with closing(http.client.HTTPConnection(*server.server_address, timeout=3)) as fresh:
            fresh.request("GET", "/v1/health")
            response = fresh.getresponse()
            assert response.status == 200
            response.read()
    finally:
        stop.set()
        sending.join(timeout=3)
        client.close()
        server.shutdown()
        server.server_close()
        serving.join(timeout=3)


def test_persistent_requests_receive_independent_input_deadlines(tmp_path, monkeypatch):
    gateway = Gateway(str(tmp_path), token=TOKEN)
    handle = gateway.handle

    def delayed(*args, **kwargs):
        # Application work must not consume the next request's input budget.
        time.sleep(0.6)
        return handle(*args, **kwargs)

    monkeypatch.setattr(gateway, "handle", delayed)
    server = make_http_server(gateway, "127.0.0.1", 0, request_timeout=1)
    serving = threading.Thread(target=server.serve_forever, daemon=True)
    serving.start()
    try:
        with closing(http.client.HTTPConnection(*server.server_address, timeout=3)) as client:
            client.connect()
            original_socket = client.sock
            for _ in range(3):
                client.request("GET", "/v1/health")
                response = client.getresponse()
                assert response.status == 200
                response.read()
            # The same socket served all requests rather than recovering by reconnecting.
            assert client.sock is original_socket
    finally:
        server.shutdown()
        server.server_close()
        serving.join(timeout=3)
