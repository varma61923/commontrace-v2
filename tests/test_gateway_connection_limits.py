"""HTTP connection admission, persistent framing, and worker cleanup."""
import shutil
import socket
import ssl
import subprocess  # nosec B404 - creates only a throwaway local test certificate
import threading
import time

import pytest

from commontrace import gateway, gateway_transport


def wait_until(predicate):
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.005)
    pytest.fail("connection state did not settle")


@pytest.fixture
def server(tmp_path):
    app = gateway.Gateway(str(tmp_path / "store"), token="t" * 40)
    srv = gateway.make_http_server(app, "127.0.0.1", 0, request_timeout=0.25, max_connections=1)
    worker = threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
    worker.start()
    yield srv
    srv.shutdown()
    srv.server_close()
    worker.join(timeout=2)


def connect(server):
    return socket.create_connection(server.server_address, timeout=2)


def receive_closed(connection):
    try:
        return connection.recv(4096)
    except ConnectionResetError:
        return b""


def assert_health_available(server):
    wait_until(lambda: server.connection_slots._value == 1)
    with connect(server) as client:
        client.sendall(b"GET /v1/health HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n")
        assert b" 200 " in client.recv(4096).split(b"\r\n")[0]


def test_idle_connection_bounds_workers_and_releases_slot_on_close(server):
    with connect(server) as idle:
        idle.sendall(b"GET /v1/health HTTP/1.1\r\n")
        wait_until(lambda: server.connection_slots._value == 0)
        with connect(server) as excess:
            assert receive_closed(excess) == b""
        idle.shutdown(socket.SHUT_WR)
    assert_health_available(server)


def test_timeout_releases_connection_admission(server):
    with connect(server) as idle:
        wait_until(lambda: server.connection_slots._value == 0)
        assert receive_closed(idle) == b""
    assert_health_available(server)


@pytest.mark.parametrize("headers, status", [
    (b"Content-Length: 2\r\nContent-Length: 2\r\n", 400),
    (b"Content-Length: +2\r\n", 400),
    (b"Content-Length: 2, 2\r\n", 400),
    (b"Transfer-Encoding: identity\r\nContent-Length: 2\r\n", 411),
    (b"Transfer-Encoding: chunked\r\nContent-Length: 2\r\n", 411),
    (b"Content-Length: " + b"9" * 5000 + b"\r\n", 413),
])
def test_ambiguous_or_unsupported_framing_closes_stream(server, headers, status):
    with connect(server) as client:
        client.sendall(b"POST /v1/recall HTTP/1.1\r\nHost: localhost\r\n" + headers + b"\r\n{}")
        assert str(status).encode() in client.recv(4096).split(b"\r\n")[0]
    assert_health_available(server)


def test_get_body_cannot_be_reinterpreted_as_a_pipelined_request(server):
    with connect(server) as client:
        client.sendall(b"GET /v1/health HTTP/1.1\r\nHost: localhost\r\nContent-Length: 2\r\n\r\n{}")
        assert b" 400 " in client.recv(4096).split(b"\r\n")[0]
    assert_health_available(server)


def test_zero_padded_length_preserves_valid_http_requests(server):
    with connect(server) as client:
        client.sendall(b"GET /v1/health HTTP/1.1\r\nHost: localhost\r\nContent-Length: "
                       + b"0" * 5000 + b"\r\nConnection: close\r\n\r\n")
        assert b" 200 " in client.recv(4096).split(b"\r\n")[0]
    assert_health_available(server)


def test_incomplete_post_body_releases_slot(server):
    with connect(server) as client:
        client.sendall(b"POST /v1/recall HTTP/1.1\r\nHost: localhost\r\n"
                       b"Content-Type: application/json\r\nContent-Length: 20\r\n\r\n{}")
        client.shutdown(socket.SHUT_WR)
        assert b" 400 " in client.recv(4096).split(b"\r\n")[0]
    assert_health_available(server)


def test_thread_start_failure_releases_slot(server, monkeypatch):
    def fail_start(self):
        raise RuntimeError("thread creation failed")

    monkeypatch.setattr(threading.Thread, "start", fail_start)
    left, right = socket.socketpair()
    try:
        with pytest.raises(RuntimeError, match="thread creation"):
            server.process_request(left, ("127.0.0.1", 1))
        assert server.connection_slots.acquire(blocking=False)
        server.connection_slots.release()
    finally:
        left.close()
        right.close()


def test_shutdown_allows_idle_worker_to_timeout_and_release_slot(server):
    with connect(server) as idle:
        wait_until(lambda: server.connection_slots._value == 0)
        server.shutdown()
        assert receive_closed(idle) == b""
        wait_until(lambda: server.connection_slots._value == 1)


@pytest.mark.skipif(shutil.which("openssl") is None, reason="requires openssl for a local test certificate")
def test_excess_tls_connection_closes_without_plaintext_response(tmp_path):
    cert, key = str(tmp_path / "cert.pem"), str(tmp_path / "key.pem")
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",  # nosec B603 B607
                    "-keyout", key, "-out", cert, "-days", "1", "-subj", "/CN=localhost"],
                   check=True, capture_output=True)
    app = gateway.Gateway(str(tmp_path / "store"), token="t" * 40)
    srv = gateway.make_http_server(app, "127.0.0.1", 0, tls=(cert, key),
                                   request_timeout=0.5, max_connections=1)
    worker = threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
    worker.start()
    try:
        with connect(srv) as idle:
            wait_until(lambda: srv.connection_slots._value == 0)
            with connect(srv) as excess:
                assert receive_closed(excess) == b""
            idle.close()
        wait_until(lambda: srv.connection_slots._value == 1)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        with context.wrap_socket(connect(srv), server_hostname="localhost") as client:
            client.sendall(b"GET /v1/health HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n")
            assert b" 200 " in client.recv(4096).split(b"\r\n")[0]
    finally:
        srv.shutdown()
        srv.server_close()
        worker.join(timeout=2)


@pytest.mark.parametrize("limit", [0, -1, True, 1.5])
def test_invalid_connection_limit_is_rejected_before_binding(tmp_path, limit):
    app = gateway.Gateway(str(tmp_path / "store"), token="t" * 40)
    with pytest.raises(ValueError, match="max_connections"):
        gateway.make_http_server(app, "127.0.0.1", 0, max_connections=limit)


@pytest.mark.parametrize("timeout", [0, -1, float("inf"), float("nan")])
def test_invalid_timeout_is_rejected_before_binding(tmp_path, timeout):
    app = gateway.Gateway(str(tmp_path / "store"), token="t" * 40)
    with pytest.raises(ValueError, match="request_timeout"):
        gateway.make_http_server(app, "127.0.0.1", 0, request_timeout=timeout)


def test_tls_setup_failure_closes_bound_server_socket(tmp_path, monkeypatch):
    sockets = []
    original_init = gateway_transport.ThreadingHTTPServer.__init__

    def capture_server(self, *args, **kwargs):
        original_init(self, *args, **kwargs)
        sockets.append(self.socket)

    monkeypatch.setattr(gateway_transport.ThreadingHTTPServer, "__init__", capture_server)
    app = gateway.Gateway(str(tmp_path / "store"), token="t" * 40)
    with pytest.raises(FileNotFoundError):
        gateway.make_http_server(app, "127.0.0.1", 0,
                                 tls=(str(tmp_path / "missing-cert.pem"), str(tmp_path / "missing-key.pem")))
    assert len(sockets) == 1
    assert sockets[0].fileno() == -1
