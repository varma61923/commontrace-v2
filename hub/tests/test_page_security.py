"""Every HTML page the Hub serves carries a Content-Security-Policy that
allows exactly its own inline scripts, and nothing else.

The policy is only useful if it matches: a script whose hash is missing
silently stops running (the double-submit guard, the auto-refresh, the
confirm prompt), and one that needs `'unsafe-inline'` to run makes the
policy decorative. So these tests hash every `<script>` a page actually
renders and hold the header to exactly that set.
"""

from __future__ import annotations

import base64
import hashlib
import pathlib
import re

import pytest
from starlette.requests import Request

from hub import admin, console, signup

HUB = pathlib.Path(__file__).resolve().parents[1]


def _script_hashes(page: str) -> set[str]:
    return {
        "'sha256-" + base64.b64encode(hashlib.sha256(body.encode()).digest()).decode() + "'"
        for body in re.findall(r"<script>(.*?)</script>", page, flags=re.S)
    }


def _directive(csp: str, name: str) -> str:
    for part in csp.split(";"):
        part = part.strip()
        if part.startswith(name + " "):
            return part[len(name) + 1:]
    raise AssertionError(f"{name} missing from {csp!r}")


PAGES = {
    "console": lambda: console._page("Proof", "<p>x</p>"),
    "console, live": lambda: console._page("Your fleet", "<p>x</p>", auto_refresh_seconds=45),
    "console, signed out": lambda: console._page("Sign in", "<p>x</p>", signed_in=False),
    "shared report": lambda: console._shared_page("<p>x</p>", expires_at=2_000_000_000),
    "operator console": lambda: admin._page("Overview", "<p>x</p>", auto_refresh_seconds=30),
    "signup": lambda: signup._page("Create account", "<p>x</p>"),
}


@pytest.mark.parametrize("name", PAGES)
def test_the_policy_allows_exactly_the_scripts_the_page_renders(name):
    response = PAGES[name]()
    page = response.body.decode()
    csp = response.headers["content-security-policy"]
    allowed = set(_directive(csp, "script-src").split())
    rendered = _script_hashes(page)
    assert allowed == (rendered or {"'none'"})
    assert "'unsafe-inline'" not in _directive(csp, "script-src")
    assert _directive(csp, "default-src") == "'none'"
    assert _directive(csp, "frame-ancestors") == "'none'"
    assert _directive(csp, "base-uri") == "'none'"
    assert "no-store" in response.headers["cache-control"]
    assert response.headers["cross-origin-opener-policy"] == "same-origin"
    assert response.headers["cross-origin-resource-policy"] == "same-origin"
    assert "camera=()" in response.headers["permissions-policy"]


def test_the_shared_report_runs_no_script_and_sends_no_referrer():
    response = console._shared_page("<p>x</p>", expires_at=2_000_000_000)
    assert "<script" not in response.body.decode()
    assert _directive(response.headers["content-security-policy"], "script-src") == "'none'"
    assert response.headers["referrer-policy"] == "no-referrer"


def test_forms_may_post_only_here_or_to_stripe():
    csp = console._page("Proof", "").headers["content-security-policy"]
    assert _directive(csp, "form-action").split() == ["'self'", "https://*.stripe.com"]


@pytest.mark.parametrize("path", sorted(p.name for p in HUB.glob("*.py")))
def test_no_page_uses_an_inline_event_handler(path):
    """The policy forbids them, so one would silently do nothing."""
    source = (HUB / path).read_text()
    assert not re.search(r"""\son(click|submit|change|input|load|focus|blur)\s*=\s*['"\\]""", source), path


def test_the_nav_marks_the_current_page():
    page = console._page("Users & roles", "").body.decode()
    assert re.search(r'<a href="[^"]*/users" aria-current=page>Users</a>', page)
    assert page.count(" aria-current=page>") == 1
    assert " aria-current=page>" not in console._page("Something else", "").body.decode()


def test_the_page_offers_a_skip_link_to_its_content():
    page = console._page("Proof", "<p>x</p>").body.decode()
    assert '<a class="skip" href="#main">' in page and '<main id="main">' in page


@pytest.mark.parametrize("render", [
    lambda s: console._page("Your fleet", "", auto_refresh_seconds=s),
    lambda s: admin._page("Overview", "", auto_refresh_seconds=s),
])
def test_a_live_page_can_be_paused(render):
    """WCAG 2.2.1: a page that reloads itself must let the reader stop it.
    The control starts hidden, so it never shows without the script that
    makes it work."""
    live = render(30).body.decode()
    assert '<button type="button" class="live-toggle" data-live-toggle hidden>' in live
    assert "ct-live-paused" in live
    still = render(0).body.decode()
    assert "data-live-toggle" not in still and "ct-live-paused" not in still


def test_a_hidden_tab_does_not_reload():
    """A console left open in a background tab re-ran its queries every
    few seconds for as long as it stayed open."""
    script = admin.auto_refresh_script(30)
    assert "document.hidden" in script and "visibilitychange" in script


def _request(method: str, **headers: str) -> Request:
    return Request({
        "type": "http", "method": method, "path": "/app/keys/issue", "query_string": b"",
        "headers": [(k.lower().replace("_", "-").encode(), v.encode()) for k, v in headers.items()],
    })


@pytest.mark.parametrize(("method", "headers", "refused"), [
    ("POST", {"sec_fetch_site": "same-origin"}, False),
    ("POST", {"sec_fetch_site": "none"}, False),  # typed or bookmarked by the user
    ("POST", {"sec_fetch_site": "same-site"}, True),  # a sibling subdomain: gets the cookie
    ("POST", {"sec_fetch_site": "cross-site"}, True),
    ("POST", {"sec_fetch_site": "same-origin", "origin": "https://evil.example"}, False),  # the browser's word wins
    ("POST", {"origin": "https://hub.example.com", "host": "hub.example.com"}, False),  # older browser, own page
    ("POST", {"origin": "https://HUB.example.com", "host": "hub.example.com"}, False),
    ("POST", {"origin": "https://evil.example", "host": "hub.example.com"}, True),
    ("POST", {"origin": "null", "host": "hub.example.com"}, True),  # a sandboxed frame or data: page
    ("POST", {"host": "hub.example.com"}, False),  # not a browser: the session cookie decides
    ("GET", {"sec_fetch_site": "cross-site"}, False),  # a link from elsewhere is fine
])
def test_a_state_changing_request_from_another_origin_is_refused(method, headers, refused):
    assert admin.cross_origin_refused(_request(method, **headers)) is refused


@pytest.mark.parametrize("module", [console, signup])
def test_every_form_post_route_is_guarded(module):
    """A POST route registered without the guard would be the one a forged
    form targets."""
    source = pathlib.Path(module.__file__).read_text()
    routes = source.split("app.add_route(")[1:]
    posts = [r.split("\n\n", 1)[0] for r in routes if '"POST"' in r.split("methods=", 1)[1].split("]", 1)[0]]
    assert posts
    assert all("refuse_cross_origin(" in route for route in posts)
