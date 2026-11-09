"""HTTP refusal bodies remain owned resources, including status-only paths."""

import io
import urllib.error
from types import SimpleNamespace

import pytest

from benchmarks.dolphinbench import unimem_harness
from unimem import conformance
from unimem.connectors import web_crawler
from unimem.robotics import bridge


def test_robot_redirect_refusal_closes_the_original_response():
    body = io.BytesIO(b"redirect")
    with pytest.raises(ValueError, match="redirects are refused"):
        bridge._NoRedirect().redirect_request(None, body, 307, "redirect", {}, "https://other-origin")
    assert body.closed


def test_redirect_refuses_non_ip_socket_address_families(monkeypatch):
    monkeypatch.setattr(web_crawler.socket, "getaddrinfo", lambda *args, **kwargs: [(0, 1, 0, "", (123, b"address"))])
    assert not web_crawler._redirect_target_is_public("https://example.com/page")


@pytest.mark.parametrize("status", [404, 503])
def test_crawler_closes_http_refusals_before_propagating_them(monkeypatch, status):
    body = io.BytesIO(b"refused")
    error = urllib.error.HTTPError("https://example.com", status, "refused", {}, body)

    def fail(*args, **kwargs):
        raise error

    monkeypatch.setattr(web_crawler, "_OPENER", SimpleNamespace(open=fail))
    with pytest.raises(urllib.error.HTTPError) as caught:
        web_crawler._get("https://example.com", "test", 1, 1024)
    assert caught.value.code == status and body.closed


@pytest.mark.parametrize("status,allowed", [(401, False), (403, False), (404, True)])
def test_robots_status_checks_close_the_refusal(monkeypatch, status, allowed):
    body = io.BytesIO(b"refused")
    error = urllib.error.HTTPError("https://example.com/robots.txt", status, "refused", {}, body)

    def fail(*args, **kwargs):
        raise error

    monkeypatch.setattr(web_crawler, "_OPENER", SimpleNamespace(open=fail))
    assert web_crawler._robots_allowed("https://example.com/page", "test", 1) is allowed
    assert body.closed


@pytest.mark.parametrize("payload,expected", [(b'{"error":"refused"}', {"error": "refused"}), (b"invalid", {})])
def test_conformance_closes_error_responses_for_valid_and_invalid_json(monkeypatch, payload, expected):
    body = io.BytesIO(payload)
    error = urllib.error.HTTPError("http://localhost", 400, "refused", {}, body)

    def fail(*args, **kwargs):
        raise error

    monkeypatch.setattr(conformance.urllib.request, "build_opener", lambda *args: SimpleNamespace(open=fail))
    assert conformance._call("http://localhost", "GET", "/", None) == (400, expected)
    assert body.closed


def test_conformance_bounds_and_closes_large_error_responses(monkeypatch):
    reads = []

    class Body(io.BytesIO):
        def read(self, size=-1):
            reads.append(size)
            return super().read(size)

    body = Body(b"x" * (conformance.MAX_GATEWAY_RESPONSE_BYTES + 2))
    error = urllib.error.HTTPError("http://localhost", 400, "refused", {}, body)

    def fail(*args, **kwargs):
        raise error

    monkeypatch.setattr(conformance.urllib.request, "build_opener", lambda *args: SimpleNamespace(open=fail))
    assert conformance._call("http://localhost", "GET", "/", None) == (400, {})
    assert reads == [conformance.MAX_GATEWAY_RESPONSE_BYTES + 1] and body.closed


def test_model_refusals_are_bounded_and_closed_before_retry(monkeypatch):
    reads = []

    class Body(io.BytesIO):
        def read(self, size=-1):
            reads.append(size)
            return super().read(size)

    body = Body(b"x" * 100000)
    calls = []

    def request(*args, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            raise urllib.error.HTTPError("https://example.com", 429, "limited", {}, body)
        assert body.closed
        return io.BytesIO(b'{"ok":true}')

    monkeypatch.setattr(unimem_harness.urllib.request, "urlopen", request)
    monkeypatch.setattr(unimem_harness.time, "sleep", lambda delay: None)
    assert unimem_harness._post("https://example.com", {}, {}, 1, 1) == {"ok": True}
    assert calls == [1, 1] and reads == [4096]


@pytest.mark.parametrize("status", [400, 409, 413, 422, 401, 429, 503])
def test_robot_gateway_closes_http_errors_on_refusal_and_retry_paths(monkeypatch, status):
    body = io.BytesIO(b'{"error":{"code":"rejected"}}')
    error = urllib.error.HTTPError("https://example.com/v1/outcome", status, "refused", {}, body)

    def fail(*args, **kwargs):
        raise error

    monkeypatch.setattr(bridge, "build_opener", lambda *args: SimpleNamespace(open=fail))
    expected = bridge.GatewayRefused if status in bridge.REFUSALS else ConnectionError
    with pytest.raises(expected):
        bridge.HTTPGateway("https://example.com", "token").post("/v1/outcome", {"occasion_id": "o1"})
    assert body.closed
