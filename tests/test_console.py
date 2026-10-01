"""The local console the gateway serves (commontrace/ui/).

It is plain files with no build step, so the contract worth pinning is what a
browser may do with them: nothing inline, nothing that turns an untrusted memory
or agent id into markup, and the token never leaving the page's own origin.
"""
import os
import re

import pytest

from commontrace import gateway, holdout_io

UI_DIR = os.path.join(os.path.dirname(gateway.__file__), "ui")
FILES = ("index.html", "app.js", "app.css", "favicon.svg")


def _read(name):
    with open(os.path.join(UI_DIR, name), encoding="utf-8") as fh:
        return fh.read()


@pytest.fixture
def gw(tmp_path):
    root = str(tmp_path / "store")
    holdout_io.configure(root, rate=0.5, salt="console")
    return gateway.Gateway(root, token="t" * 40)


@pytest.mark.parametrize("path,ctype", [
    ("/", "text/html"), ("/index.html", "text/html"), ("/ui/app.js", "text/javascript"),
    ("/ui/app.css", "text/css"), ("/ui/favicon.svg", "image/svg+xml")])
def test_the_console_loads_without_a_token_and_carries_a_strict_csp(gw, path, ctype):
    # The page has to load before it can ask for the token; the data behind it still needs one.
    r = gw.handle("GET", path, {"Host": "localhost:8787"})
    assert r.status == 200 and r.content_type.startswith(ctype)
    csp = r.headers["Content-Security-Policy"]
    assert "default-src 'none'" in csp and "script-src 'self'" in csp and "frame-ancestors 'none'" in csp
    assert "unsafe-inline" not in csp and "unsafe-eval" not in csp
    assert r.headers["X-Frame-Options"] == "DENY" and r.headers["Referrer-Policy"] == "no-referrer"
    assert gw.handle("GET", "/v1/status", {"Host": "localhost:8787"}).status == 401


def test_only_the_listed_files_are_served(gw):
    for path in ("/ui/../gateway.py", "/ui/", "/ui/app.js/../../gateway.py", "/ui/secret", "/memory/gateway.token"):
        assert gw.handle("GET", path, {"Host": "localhost"}).status == 404, path


def test_the_console_answers_only_to_the_gateways_own_host_names(gw):
    assert gw.handle("GET", "/", {"Host": "evil.example"}).status == 403


def test_every_file_the_page_references_exists_and_is_same_origin():
    html = _read("index.html")
    refs = re.findall(r'(?:src|href)="([^"#][^"]*)"', html)
    assert refs, "index.html references nothing"
    for ref in refs:
        assert ref.startswith("/ui/"), ref
        assert os.path.isfile(os.path.join(UI_DIR, ref[len("/ui/"):])), ref


def test_the_page_has_no_inline_script_style_or_handlers():
    html = _read("index.html")
    assert not re.search(r"<script(?![^>]*\bsrc=)", html)
    assert "<style" not in html and " style=" not in html
    assert not re.search(r"\son[a-z]+\s*=", html)


def test_script_never_turns_strings_into_markup_or_code():
    js = re.sub(r"/\*.*?\*/|(?<![:\"'])//[^\n]*", "", _read("app.js"), flags=re.S)  # comments may name the rule
    for banned in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "new Function",
                   "setAttribute(\"style\"", "setAttribute('style'", "srcdoc"):
        assert banned not in js, banned
    assert not re.search(r"""\bstyle\s*:\s*["']""", js), "an inline style string would be refused by the CSP"


def test_the_token_comes_from_the_fragment_and_is_scrubbed_from_the_address_bar():
    js = _read("app.js")
    assert "#token=" in js or "token=" in js
    assert "sessionStorage" in js and "replaceState" in js
    # never persisted past the tab, never put in a URL, never sent anywhere but this origin
    assert "localStorage.setItem(\"token\"" not in js and "localStorage.setItem('token'" not in js
    assert not re.search(r"""fetch\(\s*["']https?://""", js)
    assert "?token=" not in js


def test_the_console_ships_in_the_package():
    with open(os.path.join(os.path.dirname(UI_DIR), "..", "pyproject.toml"), encoding="utf-8") as fh:
        assert '"ui/*"' in fh.read()
    for name in FILES:
        assert os.path.isfile(os.path.join(UI_DIR, name)), name


def test_the_colours_are_defined_for_light_dark_and_forced_colours():
    css = _read("app.css")
    assert "prefers-color-scheme: dark" in css and "forced-colors" in css and 'data-theme="dark"' in css
    assert "min-height: 44px" in css  # touch targets, for a tablet on a robot or a phone on a shop floor


def test_in_a_real_browser_hostile_ids_are_text_and_the_token_leaves_the_address_bar(tmp_path):
    """Skipped where Playwright or a Chromium is not installed (it is not a dependency)."""
    sync_api = pytest.importorskip("playwright.sync_api")
    exe = next((p for p in (os.environ.get("CHROMIUM_PATH"), "/opt/pw-browsers/chromium") if p and os.path.isfile(p)), None)
    if exe is None:
        pytest.skip("no Chromium available")
    import json
    import threading
    import urllib.request

    root = str(tmp_path / "store")
    holdout_io.configure(root, rate=0.5, salt="console-browser")
    g = gateway.Gateway(root, token="t" * 40)
    server = gateway.make_http_server(g, "127.0.0.1", 0)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        hostile = "<img src=x onerror=window.__pwned=1>"
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/v1/recall", method="POST",
            data=json.dumps({"occasion_id": hostile, "agent_id": "a-1", "items": [{"id": hostile, "text": "x"}]}).encode(),
            headers={"Authorization": "Bearer " + "t" * 40, "Content-Type": "application/json",
                     "Host": f"127.0.0.1:{port}"})
        urllib.request.urlopen(req).read()
        with sync_api.sync_playwright() as p:
            browser = p.chromium.launch(executable_path=exe)
            page = browser.new_page()
            errors = []
            page.on("pageerror", lambda e: errors.append(str(e)))
            page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
            page.goto(f"http://127.0.0.1:{port}/#token=" + "t" * 40)
            page.wait_for_selector("#nav a")
            page.goto(f"http://127.0.0.1:{port}/#/live")
            page.wait_for_selector(".events li")
            assert page.evaluate("window.__pwned") is None
            assert page.evaluate("document.querySelectorAll('main img').length") == 0
            assert hostile in page.inner_text("main")
            assert "token" not in page.url
            assert errors == []
            browser.close()
    finally:
        server.shutdown()


def _workbench_store(tmp_path):
    from commontrace import frontmatter, paths, templates
    from commontrace.cli import main
    main(["init", "--agent-type", "support", "--dest", str(tmp_path)])
    holdout_io.configure(str(tmp_path), rate=0.5, salt="console-wb")
    for slug, rule in (("lesson_ready", "Link the single refund policy page."),
                       ("lesson_todo", "TODO: one actionable sentence")):
        fm = templates.lesson_frontmatter(
            slug=slug, description=f"{slug} description", agent_type="support", domain="refunds", tags=[],
            applies_when="A customer asks about refunds." if slug == "lesson_ready" else "TODO: when",
            do_not_apply_when="Already refunded." if slug == "lesson_ready" else "TODO: when not", importance=3,
            importance_rationale="r", source_traces=["t1"], status="review")
        body = (f"## Rule\n{rule}\n\n## Why\nw\n\n## How to apply\n{fm['applies_when']}\n\n"
                f"## Counter-examples\n{fm['do_not_apply_when']}\n")
        frontmatter.write(os.path.join(paths.lessons_dir(str(tmp_path)), f"{slug}.md"), fm, body)


def test_in_a_real_browser_a_draft_is_edited_approved_and_a_failing_one_cannot_be_selected(tmp_path):
    """Skipped where Playwright or a Chromium is not installed."""
    sync_api = pytest.importorskip("playwright.sync_api")
    exe = next((p for p in (os.environ.get("CHROMIUM_PATH"), "/opt/pw-browsers/chromium") if p and os.path.isfile(p)),
               None)
    if exe is None:
        pytest.skip("no Chromium available")
    import threading

    from commontrace import frontmatter, paths

    _workbench_store(tmp_path)
    g = gateway.Gateway(str(tmp_path), token="t" * 40, allow_approval=True)
    server = gateway.make_http_server(g, "127.0.0.1", 0)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        with sync_api.sync_playwright() as p:
            browser = p.chromium.launch(executable_path=exe)
            page = browser.new_page()
            errors = []
            page.on("pageerror", lambda e: errors.append(str(e)))
            page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
            page.goto(f"http://127.0.0.1:{port}/#token=" + "t" * 40)
            page.goto(f"http://127.0.0.1:{port}/#/review")
            page.wait_for_selector(".queue li")
            assert page.is_disabled("#sel-lesson_todo") and not page.is_disabled("#sel-lesson_ready")
            page.click("a.q-name:has-text('lesson_todo')")
            page.wait_for_selector("#f-rule")
            assert page.is_disabled("button:has-text('Approve')")            # the gates say no
            page.fill("#f-rule", "Always link the refund policy page.")
            page.fill("#f-applies_when", "A customer asks when a refund arrives.")
            page.fill("#f-do_not_apply_when", "The refund was already issued.")
            page.click("button:has-text('Save changes')")
            page.wait_for_selector("text=Saved. The gates below were re-checked.")
            page.wait_for_selector("button:has-text('Approve'):not([disabled])")
            page.fill("#why", "read and agree")
            page.click("button:has-text('Approve')")
            page.wait_for_selector(".queue li")
            fm = frontmatter.read(os.path.join(paths.lessons_dir(str(tmp_path)), "lesson_todo.md"))[0]
            assert fm["status"] == "active" and errors == []
            browser.close()
    finally:
        server.shutdown()
