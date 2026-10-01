"""Over TLS, a client that connects and never handshakes must not stall anyone else."""
import shutil
import socket
import ssl
import subprocess  # nosec B404 - test fixture: a throwaway self-signed certificate
import threading
import urllib.request

import pytest

from commontrace import gateway

pytestmark = pytest.mark.skipif(shutil.which("openssl") is None, reason="needs the openssl binary for a test cert")


@pytest.fixture
def tls_server(tmp_path):
    cert, key = str(tmp_path / "c.pem"), str(tmp_path / "k.pem")
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", key, "-out", cert,  # nosec
                    "-days", "1", "-subj", "/CN=localhost"], check=True, capture_output=True)
    g = gateway.Gateway(str(tmp_path / "store"), token="t" * 40)
    server = gateway.make_http_server(g, "127.0.0.1", 0, tls=(cert, key), request_timeout=2.0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server.server_address[1]
    server.shutdown()
    server.server_close()


def test_a_silent_connection_does_not_block_the_next_client(tls_server):
    idle = socket.create_connection(("127.0.0.1", tls_server))
    try:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        with urllib.request.urlopen(f"https://127.0.0.1:{tls_server}/", context=ctx, timeout=1.5) as resp:
            assert resp.status == 200
    finally:
        idle.close()


def test_a_plaintext_client_on_the_tls_port_is_dropped_and_the_server_keeps_serving(tls_server):
    with socket.create_connection(("127.0.0.1", tls_server), timeout=2) as raw:
        raw.sendall(b"GET / HTTP/1.1\r\nHost: localhost\r\n\r\n")
        try:
            raw.recv(1024)  # a TLS alert or a close; either way, no HTTP answer in the clear
        except OSError:
            pass
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    with urllib.request.urlopen(f"https://127.0.0.1:{tls_server}/", context=ctx, timeout=1.5) as resp:
        assert resp.status == 200
