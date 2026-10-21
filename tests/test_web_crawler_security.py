"""Network-boundary regressions for the web crawler."""
from __future__ import annotations

import io
import socket
import urllib.request

import pytest

from unimem import offline
from unimem.connectors import web_crawler


@pytest.mark.parametrize("url", [
    "file:///etc/passwd", "ftp://example.com/data", "http:///missing-host",
    "https://user:secret@example.com/", "http://example.com:99999/",
    "http://example.com:\\bad/", "https://example.com/\nheader",
])
def test_get_rejects_unsafe_urls_before_opening(monkeypatch, url):
    monkeypatch.delenv(offline.ENV, raising=False)

    def unexpected_open(*args, **kwargs):
        pytest.fail("invalid URL reached the network")

    monkeypatch.setattr(web_crawler._OPENER, "open", unexpected_open)
    with pytest.raises(ValueError):
        web_crawler._get(url, "bot", 1, 10)


def test_offline_policy_applies_to_redirects_before_dns(monkeypatch):
    monkeypatch.setenv(offline.ENV, "1")

    def unexpected_dns(*args, **kwargs):
        pytest.fail("offline redirect attempted DNS")

    monkeypatch.setattr(socket, "getaddrinfo", unexpected_dns)
    request = urllib.request.Request("http://127.0.0.1/start")
    with pytest.raises(offline.OfflineError):
        web_crawler._HttpOnlyRedirects().redirect_request(
            request, None, 302, "Found", {}, "https://example.com/remote")


@pytest.mark.parametrize("target", [
    "http://169.254.169.254/latest/meta-data/", "http://10.0.0.1/admin",
    "http://127.0.0.1/private", "http://[::1]/", "http://localhost/private",
    "http://[ff0e::1]/", "http://[fec0::1]/",
])
def test_cross_origin_redirects_cannot_introduce_internal_targets(monkeypatch, target):
    monkeypatch.delenv(offline.ENV, raising=False)
    request = urllib.request.Request("https://public.example/start")
    with pytest.raises(ValueError, match="non-public"):
        web_crawler._HttpOnlyRedirects().redirect_request(request, None, 302, "Found", {}, target)


def test_redirect_checks_every_dns_answer(monkeypatch):
    monkeypatch.delenv(offline.ENV, raising=False)
    monkeypatch.setattr(socket, "getaddrinfo", lambda *args, **kwargs: [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443)),
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.1", 443)),
    ])
    request = urllib.request.Request("https://public.example/start")
    with pytest.raises(ValueError, match="non-public"):
        web_crawler._HttpOnlyRedirects().redirect_request(
            request, None, 302, "Found", {}, "https://mixed.example/page")


def test_public_redirect_strips_credentials(monkeypatch):
    monkeypatch.delenv(offline.ENV, raising=False)
    monkeypatch.setattr(web_crawler, "_redirect_target_is_public", lambda url: True)
    request = urllib.request.Request("https://public.example/start", headers={
        "Authorization": "Bearer secret", "Cookie": "session=secret", "User-Agent": "bot",
        "Proxy-Authorization": "Basic secret",
    })
    redirected = web_crawler._HttpOnlyRedirects().redirect_request(
        request, None, 302, "Found", {}, "https://other.example/page")
    assert redirected.get_header("Authorization") is None
    assert redirected.get_header("Cookie") is None
    assert redirected.get_header("Proxy-authorization") is None
    assert redirected.get_header("User-agent") == "bot"


def test_local_same_origin_redirect_is_preserved_offline(monkeypatch):
    monkeypatch.setenv(offline.ENV, "1")
    request = urllib.request.Request("http://127.0.0.1:8000/start")
    redirected = web_crawler._HttpOnlyRedirects().redirect_request(
        request, None, 302, "Found", {}, "http://127.0.0.1:8000/page")
    assert redirected.full_url == "http://127.0.0.1:8000/page"


@pytest.mark.parametrize("size,accepted", [(9, True), (10, True), (11, False)])
def test_response_limit_is_enforced_without_silent_truncation(monkeypatch, size, accepted):
    response = io.BytesIO(b"x" * size)
    response.headers = {"Content-Type": "text/plain"}
    monkeypatch.setattr(web_crawler._OPENER, "open", lambda *args, **kwargs: response)
    if accepted:
        assert web_crawler._get("http://localhost/page", "bot", 1, 10)[1] == b"x" * size
    else:
        with pytest.raises(ValueError, match="size limit"):
            web_crawler._get("http://localhost/page", "bot", 1, 10)


def test_oversized_robots_fails_closed(monkeypatch):
    def oversized(*args, **kwargs):
        raise ValueError("web crawling response exceeds size limit")

    monkeypatch.setattr(web_crawler, "_get", oversized)
    assert not web_crawler._robots_allowed("https://example.com/private", "bot", 1)


class TestHtmlConversionIsLinear:
    """Hostile pages must not turn the converter quadratic: the regex version
    rescanned to the end of the page from every unclosed opener and needed
    minutes for one 512 KB page of `<script>` or `<!--`."""

    @pytest.mark.parametrize("unit", ["<script>", "<!--", "<h1>", "<", "<!x", "<style a='"])
    def test_a_maximum_size_hostile_page_converts_quickly(self, unit):
        import time

        from unimem.connectors.web_crawler import MAX_PAGE_BYTES, _html_to_markdown_text

        page = unit * (MAX_PAGE_BYTES // len(unit))
        started = time.perf_counter()
        _html_to_markdown_text(page)
        assert time.perf_counter() - started < 5.0

    def test_the_markdown_shape_is_unchanged(self):
        from unimem.connectors.web_crawler import _html_to_markdown_text

        page = (
            "<!DOCTYPE html><head><style>b{}</style><script>var a='<p>x</p>'</script></head>"
            "<h1 class='t'>Main <b>Title</b></h1><p>Fish &amp; chips</p><!-- gone -->"
            "<div>Block<br>line</div><H2>Sub</H2><noscript>js</noscript>tail"
        )
        # Byte-for-byte what the previous regex implementation produced.
        assert _html_to_markdown_text(page) == "# Main Title\n\nFish & chips\nBlock\nline\n## Sub\n\ntail"

    def test_an_unclosed_script_hides_the_rest_of_the_page_like_a_browser(self):
        from unimem.connectors.web_crawler import _html_to_markdown_text

        assert _html_to_markdown_text("before<script>steal(document.cookie) never closed") == "before"

    def test_offsets_survive_text_whose_lowercase_changes_length(self):
        from unimem.connectors.web_crawler import _html_to_markdown_text

        # "İ".lower() is two code points; offsets from a str.lower() copy
        # would drift and cut the page in the wrong places.
        page = "İ" * 50 + "<script>hidden</script><h1>Kept</h1>"
        assert _html_to_markdown_text(page) == "İ" * 50 + "\n\n# Kept"
