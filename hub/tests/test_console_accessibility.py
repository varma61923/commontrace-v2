"""Real Chromium contracts for the production HTML, CSP, styles and interactions.

These component tests need no database. Authenticated server-form workflows are
covered against PostgreSQL in test_console_learning.py. Optionally set
UNIMEM_AXE_SCRIPT to a local axe-core script to run its rules on these states.
"""

from __future__ import annotations

import os
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

import pytest

from hub import console

pw = pytest.importorskip("playwright.sync_api")


@pytest.fixture(scope="module")
def browser():
    with pw.sync_playwright() as runtime:
        try:
            instance = runtime.chromium.launch(headless=True)
        except pw.Error as exc:
            if "Executable doesn't exist" in str(exc) and not os.environ.get("UNIMEM_REQUIRE_BROWSER"):
                pytest.skip("Install Playwright Chromium to verify console accessibility")
            raise
        try:
            yield instance
        finally:
            instance.close()


@pytest.fixture(scope="module")
def preview():
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            path = urlsplit(self.path).path
            title = dict((console.CONSOLE_PATH + path, title) for path, _, title in console._NAV).get(path, "Learning")
            data = {
                "experiment_id": "browser-fixture",
                "config_revision": 1,
                "decision_revision": 2,
                "config": {"mode": "observe", "holdout_rate": 0.5, "min_arm": 10},
                "occasions": 120,
                "outcomes": 100,
                "report": {"evidence": 120},
            }
            body = console._render_learning(None if "onboarding" in self.path else data, True)
            if title != "Learning":
                body = (
                    f'<h1>{console.h(title)}</h1><label for="preview-filter">Filter rows</label>'
                    '<input id="preview-filter">'
                )
            token = console._VIEW.set({"org_name": "Browser fixture", "is_admin": True})
            try:
                response = console._page(title, body)
            finally:
                console._VIEW.reset(token)
            self.send_response(response.status_code)
            for name, value in response.headers.items():
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(response.body)

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)
        assert not thread.is_alive()


def assert_axe(page):
    script = os.environ.get("UNIMEM_AXE_SCRIPT")
    if script:
        page.evaluate(Path(script).read_text())
        violations = page.evaluate("""async()=>{const r=await axe.run(document,{runOnly:{type:'tag',
          values:['wcag2a','wcag2aa','wcag21a','wcag21aa','wcag22a','wcag22aa','best-practice']}});
          return r.violations.map(v=>({id:v.id,targets:v.nodes.map(n=>n.target)}));}""")
        assert not violations


@pytest.mark.parametrize("width", [390, 1440])
def test_palette_keyboard_results_empty_state_and_focus(browser, preview, width):
    context = browser.new_context(viewport={"width": width, "height": 844}, reduced_motion="reduce")
    try:
        page = context.new_page()
        page.goto(preview + "/app/learning")
        launcher = page.get_by_role("button", name=re.compile("Search or jump to"))
        launcher.click()
        search = page.get_by_role("combobox", name="Search commands")
        pw.expect(search).to_be_focused()
        pw.expect(page.get_by_role("listbox", name="Commands")).to_be_visible()
        pw.expect(page.get_by_role("listbox").get_by_role("option", selected=True)).to_have_count(1)
        first = search.get_attribute("aria-activedescendant")
        search.press("ArrowDown")
        assert search.get_attribute("aria-activedescendant") != first
        pw.expect(page.locator("#" + search.get_attribute("aria-activedescendant"))).to_be_visible()
        assert_axe(page)
        for _ in range(20):
            search.press("ArrowDown")
        search.press("Escape")
        page.keyboard.press("Control+k")
        pw.expect(page.get_by_role("listbox").get_by_role("option", selected=True)).to_be_in_viewport(ratio=1)
        search.fill("zzzz-no-matching-command")
        pw.expect(page.get_by_role("status")).to_contain_text("No matching commands")
        pw.expect(search).to_have_attribute("aria-expanded", "false")
        assert search.get_attribute("aria-activedescendant") is None
        pw.expect(page.get_by_role("listbox")).to_have_count(0)
        search.press("ArrowDown")
        search.press("ArrowUp")
        search.press("Enter")
        pw.expect(search).to_be_focused()
        assert page.url.endswith("/app/learning")
        assert_axe(page)
        search.fill("Audit log")
        pw.expect(page.get_by_role("status")).to_contain_text("1 command available")
        search.press("Home")
        assert search.evaluate("e=>e.selectionStart") == 0
        search.press("End")
        assert search.evaluate("e=>e.selectionStart") == len("Audit log")
        search.press("Escape")
        pw.expect(launcher).to_be_focused()
        page.keyboard.press("Control+k")
        search.fill("Audit log")
        search.press("Enter")
        pw.expect(page).to_have_url(preview + "/app/audit")
        pw.expect(page.get_by_role("heading", name="Audit log", exact=True)).to_be_visible()
    finally:
        context.close()


def test_mobile_current_navigation_preserves_focus_and_vertical_scroll(browser, preview):
    context = browser.new_context(viewport={"width": 1440, "height": 900}, reduced_motion="reduce")
    try:
        page = context.new_page()
        page.goto(preview + "/app/audit")
        page.set_viewport_size({"width": 390, "height": 844})
        current = page.locator(".side nav [aria-current=page]")
        pw.expect(current).to_be_in_viewport(ratio=1)
        page.evaluate("""()=>{document.querySelector('main').style.minHeight='2400px';
          document.querySelector('#preview-filter').focus({preventScroll:true});scrollTo(0,400);
          document.querySelector('.side nav').scrollLeft=0;}""")
        before = page.evaluate("()=>({y:scrollY,focus:document.activeElement.id})")
        page.evaluate("()=>window.dispatchEvent(new Event('resize'))")
        page.wait_for_function("""()=>{const n=document.querySelector('.side nav').getBoundingClientRect();
          const a=document.querySelector('.side nav [aria-current=page]').getBoundingClientRect();
          return a.left>=n.left-1&&a.right<=n.right+1;}""")
        assert page.evaluate("()=>({y:scrollY,focus:document.activeElement.id})") == before
        assert page.evaluate("()=>document.documentElement.scrollWidth<=innerWidth")
    finally:
        context.close()


def test_learning_progressive_disclosure_and_touch_targets(browser, preview):
    context = browser.new_context(viewport={"width": 390, "height": 844}, has_touch=True, reduced_motion="reduce")
    try:
        page = context.new_page()
        page.goto(preview + "/app/learning")
        assert page.evaluate("()=>matchMedia('(pointer:coarse)').matches")
        pw.expect(page.get_by_role("heading", name="Observe", exact=True)).to_be_visible()
        pw.expect(page.get_by_label("Mode", exact=True)).not_to_be_visible()
        pw.expect(page.get_by_label("Reason for rollback", exact=True)).not_to_be_visible()
        assert page.locator(".tiles").bounding_box()["y"] < 844
        page.get_by_text("Experiment configuration", exact=True).click()
        pw.expect(page.get_by_label("Mode", exact=True)).to_be_visible()
        assert page.locator(".learning-flow").is_hidden()
        for target in (
            page.locator(".icon-btn"),
            page.get_by_label("Mode", exact=True),
            page.get_by_label("Holdout rate", exact=True),
            page.locator(".learning-confirm"),
            page.get_by_role("button", name="Start new experiment", exact=True),
            page.locator(".side nav [aria-current=page]"),
        ):
            box = target.bounding_box()
            assert box["height"] >= 44 and box["width"] >= 44
        assert_axe(page)
        page.goto(preview + "/app/learning?onboarding")
        pw.expect(page.get_by_label("Mode", exact=True)).to_be_visible()
        pw.expect(page.locator(".learning-flow")).to_be_visible()
        assert page.evaluate("()=>document.documentElement.scrollWidth<=innerWidth")
    finally:
        context.close()
