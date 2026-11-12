"""Real Chromium contracts against the production console and an isolated gateway.

Core installs may omit Playwright. The dedicated CI browser job installs its
pinned browser and requires every contract here; screenshots are not persisted.
"""

from __future__ import annotations

import os
import re
import shlex
import threading
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Iterator

import pytest

from unimem import (
    frontmatter,
    functions,
    gateway,
    hierarchical,
    holdout_io,
    learning,
    memory_control,
    paths,
    proof,
    templates,
)
from unimem.fact_evidence import bind_evidence
from unimem.memory_journal import Journal
from unimem.memory_record import MemoryRecord

if TYPE_CHECKING:
    from pathlib import Path

    from playwright.sync_api import APIResponse, Browser, Page, Route

if os.environ.get("UNIMEM_REQUIRE_BROWSER"):
    import playwright.sync_api as pw
else:
    pw = pytest.importorskip("playwright.sync_api")
MARKUP = "<img src=x onerror='window.auditExecuted=true'>"
SOURCE = "Aster ledger records require a stable idempotency key before a retry."


@dataclass
class Console:
    root: str
    url: str
    credential: list[str]
    application: gateway.Gateway

    @property
    def headers(self) -> dict[str, str]:
        return {"Authorization": "Bearer " + self.credential[0]}


@pytest.fixture(scope="module")
def browser() -> Iterator[Browser]:
    with pw.sync_playwright() as runtime:
        executable = runtime.chromium.executable_path
        # Normal core installs have neither Playwright nor its browser. CI
        # explicitly requires the browser, so a broken install must fail there.
        if not os.path.isfile(executable) and not os.environ.get("UNIMEM_REQUIRE_BROWSER"):
            try:
                instance = runtime.chromium.launch(headless=True)
            except pw.Error as exc:
                if "Executable doesn't exist" in str(exc):
                    pytest.skip("Install Chromium with python -m playwright install chromium --only-shell")
                raise
        else:
            instance = runtime.chromium.launch(headless=True)
        try:
            yield instance
        finally:
            instance.close()


@pytest.fixture
def console(tmp_path: Path) -> Iterator[Console]:
    root = str(tmp_path / "store")
    first, _ = hierarchical.add_fact(root, SOURCE)
    second, _ = hierarchical.add_fact(
        root, "Aster billing workers preserve the payment request identifier across timeouts."
    )
    receipts = [bind_evidence(root, "fact", source.id) for source in (first, second)]
    hierarchical.add_fact(
        root,
        "Aster payment retries reuse a stable idempotency key. Literal evidence " + MARKUP + ".",
        evidence=receipts,
        min_support=2,
    )
    credential = ["browser-contract-credential-first"]
    app = gateway.Gateway(
        root,
        token_provider=lambda: credential[0],
        allow_approval=True,
        durable=False,
        config=gateway.GatewayConfig(protected_prefixes=("safety/",)),
    )
    server = gateway.make_http_server(app, "127.0.0.1", 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield Console(root, f"http://127.0.0.1:{server.server_address[1]}", credential, app)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        assert not thread.is_alive()


@pytest.fixture
def page(browser: Browser) -> Iterator[Page]:
    context = browser.new_context(viewport={"width": 1440, "height": 1000}, reduced_motion="reduce")
    tab = context.new_page()
    tab.set_default_timeout(10_000)
    try:
        yield tab
    finally:
        context.close()


def connect(page: Page, console: Console) -> None:
    page.goto(console.url + "/", wait_until="domcontentloaded")
    page.get_by_label("Access token", exact=True).fill(console.credential[0])
    page.get_by_role("button", name="Connect", exact=True).click()
    pw.expect(page.locator("#conn")).to_have_attribute("data-state", "ok")


def assert_accessible(page: Page) -> None:
    script = os.environ.get("UNIMEM_AXE_SCRIPT")
    if not script:
        return
    with open(script, encoding="utf-8") as source:
        page.evaluate(source.read())
    violations = page.evaluate("""async()=>{const r=await axe.run(document,{runOnly:{type:'tag',
        values:['wcag2a','wcag2aa','wcag21a','wcag21aa','wcag22a','wcag22aa','best-practice']}});
        return r.violations.map(v=>({id:v.id,targets:v.nodes.map(n=>n.target)}));}""")
    assert violations == []


def navigate(page: Page, label: str) -> None:
    menu = page.get_by_role("button", name="Menu", exact=True)
    if menu.is_visible() and menu.get_attribute("aria-expanded") != "true":
        menu.click()
    page.get_by_role("navigation", name="Console sections").get_by_role("link", name=label, exact=True).click()
    heading = {
        "Review": "Review queue",
        "Safety": "Safety and policy",
        "Fleet": "Robots and agents",
        "Memories": "What each memory did",
        "Live": "Live activity",
    }.get(label, label)
    pw.expect(page.get_by_role("heading", name=heading, exact=True)).to_be_visible()


def open_learning_panel(page: Page, label: str) -> None:
    summary = page.get_by_text(label, exact=True)
    pw.expect(summary).to_be_visible()
    if summary.locator("..").get_attribute("open") is None:
        summary.click()


def draft(console: Console, slug: str, description: str = "Prevent duplicate payment charges after a timeout") -> None:
    fm = templates.lesson_frontmatter(
        slug=slug,
        description=description,
        agent_type="support",
        domain="payments",
        tags=["pay"],
        applies_when="Payments are retried after a timeout.",
        do_not_apply_when="The call is naturally idempotent.",
        importance=3,
        importance_rationale="Prevent duplicate customer charges",
        source_traces=["trace-1", "trace-2"],
        status="review",
    )
    body = (
        "## Rule\nSend an idempotency key on every payment request.\n\n## Why\nPrevent duplicate charges.\n\n"
        "## How to apply\nPersist a stable request identifier before retrying.\n\n"
        "## Counter-examples\nThe call is naturally idempotent.\n"
    )
    frontmatter.write(os.path.join(paths.lessons_dir(console.root), slug + ".md"), fm, body)


@pytest.mark.parametrize("width", [390, 1440])
def test_memory_palace_guarded_actions_and_literal_evidence(page: Page, console: Console, width: int) -> None:
    page.set_viewport_size({"width": width, "height": 1000})
    memory_control.directive(console.root, "Do not deploy " + MARKUP, deny_tools=["deploy"])
    memory_control.standing_question(console.root, "Which release risks need attention?")
    suggestion = memory_control.proposal(console.root, "Inspect a suggested procedure", sources=["trace-1"])
    connect(page, console)
    navigate(page, "Memory Palace")
    pw.expect(page.get_by_text("Do not deploy " + MARKUP, exact=True)).to_be_visible()
    assert page.locator("main img").count() == 0
    page.get_by_role("button", name="Refresh answer", exact=True).click()
    pw.expect(page.get_by_role("button", name="Refresh queued", exact=True)).to_be_visible()
    page.get_by_text("Inspect evidence and applicability", exact=True).click()
    page.once("dialog", lambda dialog: dialog.accept("Insufficient supporting evidence"))
    page.get_by_role("button", name="Reject suggestion", exact=True).click()
    pw.expect(page.get_by_role("button", name="Rejected", exact=True)).to_be_visible()
    revision = next(row for row in memory_control.records(console.root, "proposal") if row["id"] == suggestion["id"])
    assert revision["data"]["status"] == "archived"
    assert revision["data"]["reason"] == "Insufficient supporting evidence"
    assert not page.evaluate("() => document.documentElement.scrollWidth > innerWidth")


@pytest.mark.parametrize("width", [390, 1440])
def test_navigation_keyboard_theme_and_literal_review_evidence(page: Page, console: Console, width: int) -> None:
    page.set_viewport_size({"width": width, "height": 1000})
    draft(console, "lesson_markup", "Literal review evidence " + MARKUP)
    errors: list[str] = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    connect(page, console)
    if page.get_by_role("button", name="Menu", exact=True).is_visible():
        page.get_by_role("button", name="Menu", exact=True).click()
    links = page.get_by_role("navigation", name="Console sections").get_by_role("link")
    for link in links.all():
        bounds = link.bounding_box()
        assert bounds is not None and bounds["x"] >= 0 and bounds["x"] + bounds["width"] <= width + 1
    page.get_by_label("Theme", exact=True).select_option("dark")
    pw.expect(page.locator("html")).to_have_attribute("data-theme", "dark")
    skip = page.get_by_role("link", name="Skip to content", exact=True)
    route_before_skip = page.url
    skip.focus()
    skip.press("Enter")
    pw.expect(page.locator("main")).to_be_focused()
    assert page.url == route_before_skip
    review = page.get_by_role("navigation", name="Console sections").get_by_role("link", name="Review", exact=True)
    review.focus()
    review.press("Enter")
    # Mobile navigation collapses after activating a route; its current-page
    # marker remains available when the disclosure is opened again.
    pw.expect(page.locator('#nav a[href="#/review"]')).to_have_attribute("aria-current", "page")
    pw.expect(page.get_by_text("Literal review evidence " + MARKUP, exact=True)).to_be_visible()
    assert page.locator("main img").count() == 0
    assert page.evaluate("() => window.auditExecuted === undefined")
    assert not page.evaluate("() => document.documentElement.scrollWidth > innerWidth")
    assert not errors


def test_skip_to_content_preserves_current_route_and_unsent_form(page: Page, console: Console) -> None:
    connect(page, console)
    navigate(page, "Explore memory")
    question = page.get_by_label("Your question", exact=True)
    question.fill("An unsent investigation must survive keyboard navigation")
    route_before_skip = page.url
    skip = page.get_by_role("link", name="Skip to content", exact=True)
    skip.focus()
    skip.press("Enter")
    pw.expect(page.locator("main")).to_be_focused()
    assert page.url == route_before_skip
    pw.expect(page.get_by_role("heading", name="Explore memory", exact=True)).to_be_visible()
    pw.expect(question).to_have_value("An unsent investigation must survive keyboard navigation")


def test_changed_backend_refresh_preserves_keyboard_navigation_focus(page: Page, console: Console) -> None:
    connect(page, console)
    experiment = page.locator("#c-exp").locator("..").locator(".big")
    pw.expect(experiment).to_have_text("Not started")
    holdout_io.configure(console.root, rate=0.2)
    review = page.get_by_role("navigation", name="Console sections").get_by_role("link", name="Review", exact=True)
    review.focus()
    # Exercise the production refresh handler, then await an observable repaint
    # caused by new backend data before checking the focused navigation link.
    page.evaluate("() => document.dispatchEvent(new Event('visibilitychange'))")
    pw.expect(experiment).to_have_text("20%")
    pw.expect(review).to_be_focused()
    review.press("Enter")
    pw.expect(review).to_have_attribute("aria-current", "page")
    pw.expect(page.get_by_role("heading", name="Review queue", exact=True)).to_be_visible()


@pytest.mark.parametrize("width", [390, 1440])
def test_explorer_retrieves_real_evidence_and_preserves_literal_markup(
    page: Page, console: Console, width: int
) -> None:
    page.set_viewport_size({"width": width, "height": 1000})
    connect(page, console)
    navigate(page, "Explore memory")
    page.get_by_label("Your question", exact=True).fill("Aster payment idempotency")
    with page.expect_response(lambda response: response.url.endswith("/v1/explore")) as response:
        page.get_by_role("button", name="Retrieve memory", exact=True).click()
    assert response.value.status == 200
    payload = response.value.json()
    assert payload["read_only"] is True and payload["items"]
    pw.expect(page.locator("#explore-results")).to_contain_text(MARKUP)
    bound = page.locator("article").filter(has_text=MARKUP)
    bound.get_by_text("Evidence and provenance", exact=True).click()
    pw.expect(bound).to_contain_text(SOURCE)
    assert page.locator("#explore-results img").count() == 0
    assert page.evaluate("() => window.auditExecuted === undefined")
    assert not page.evaluate("() => document.documentElement.scrollWidth > innerWidth")
    # Exploration must not introduce experimental occasions/outcomes.
    occasions = page.request.get(console.url + "/v1/occasions", headers=console.headers).json()
    assert occasions["events"] == []


def test_explorer_form_survives_changed_backend_poll_and_route_return(page: Page, console: Console) -> None:
    connect(page, console)
    navigate(page, "Explore memory")
    page.get_by_label("Your question", exact=True).fill("Unsubmitted Aster payment investigation")
    page.get_by_label("Context budget", exact=True).fill("777")
    page.get_by_label("Fact ranking", exact=True).select_option("bm25-v1")
    page.get_by_label("Valid on · optional", exact=True).fill("2026-01-10")
    page.get_by_label("Include conversations", exact=True).check()
    page.get_by_label("Conversation space", exact=True).fill("billing-agent")
    page.get_by_role("heading", name="Explore memory", exact=True).click()
    response = page.request.post(
        console.url + "/v1/recall", headers=console.headers, data={"occasion_id": "browser-poll", "items": []}
    )
    assert response.status == 200
    page.wait_for_event("response", predicate=lambda r: r.url.endswith("/v1/status"), timeout=12_000)
    pw.expect(page.get_by_label("Your question", exact=True)).to_have_value("Unsubmitted Aster payment investigation")
    navigate(page, "Review")
    navigate(page, "Explore memory")
    pw.expect(page.get_by_label("Context budget", exact=True)).to_have_value("777")
    pw.expect(page.get_by_label("Fact ranking", exact=True)).to_have_value("bm25-v1")
    pw.expect(page.get_by_label("Valid on · optional", exact=True)).to_have_value("2026-01-10")
    pw.expect(page.get_by_label("Include conversations", exact=True)).to_be_checked()
    pw.expect(page.get_by_label("Conversation space", exact=True)).to_have_value("billing-agent")


@pytest.mark.parametrize("width", [390, 1440])
def test_fact_ranking_selection_uses_actual_response_profile_without_relabelling_old_results(
    page: Page,
    console: Console,
    width: int,
) -> None:
    page.set_viewport_size({"width": width, "height": 1000})
    connect(page, console)
    navigate(page, "Explore memory")
    selector = page.get_by_label("Fact ranking", exact=True)
    pw.expect(selector).to_have_value("overlap-v1")
    pw.expect(selector).to_be_visible()
    bounds = selector.bounding_box()
    assert bounds is not None and bounds["height"] >= 44
    page.get_by_label("Your question", exact=True).fill("Aster payment idempotency")
    with page.expect_response(lambda response: response.url.endswith("/v1/explore")) as initial:
        page.get_by_role("button", name="Retrieve memory", exact=True).click()
    assert initial.value.status == 200 and initial.value.json()["fact_scorer"] == "overlap-v1"
    pw.expect(page.locator("#explore-results")).to_contain_text("Fact ranking used: Existing overlap")
    selector.select_option("bm25-v1")
    navigate(page, "Review")
    navigate(page, "Explore memory")
    pw.expect(selector).to_have_value("bm25-v1")
    pw.expect(page.locator("#explore-results")).to_contain_text("Fact ranking used: Existing overlap")
    with page.expect_response(lambda response: response.url.endswith("/v1/explore")) as selected:
        page.get_by_role("button", name="Retrieve memory", exact=True).click()
    assert selected.value.status == 200
    result = selected.value.json()
    assert result["fact_scorer"] == "bm25-v1"
    facts = [item for item in result["items"] if item["channel"] == "facts"]
    assert facts and all(item["provenance"]["search"]["scorer"] == "bm25-v1" for item in facts)
    assert all(item["provenance"]["search"]["matched_terms"] for item in facts)
    pw.expect(page.locator("#explore-results")).to_contain_text("Fact ranking used: BM25 · multilingual")
    fact_card = page.locator(".evidence-card").filter(has_text=facts[0]["id"])
    fact_card.get_by_text("Evidence and provenance", exact=True).click()
    pw.expect(fact_card).to_contain_text("Matched stemmed search terms:")
    assert page.locator("#explore-results img").count() == 0
    assert not page.evaluate("() => document.documentElement.scrollWidth > innerWidth")


@pytest.mark.parametrize("replacement", [False, True])
def test_pending_exploration_cannot_reappear_after_navigation_or_credential_replacement(
    page: Page,
    console: Console,
    replacement: bool,
) -> None:
    held: list[tuple[Route, APIResponse]] = []

    def hold(route: Route) -> None:
        held.append((route, route.fetch()))

    connect(page, console)
    page.route("**/v1/explore", hold)
    navigate(page, "Explore memory")
    page.get_by_label("Your question", exact=True).fill("Aster payment idempotency")
    page.get_by_role("button", name="Retrieve memory", exact=True).click()
    pw.expect(page.get_by_role("button", name="Retrieving…", exact=True)).to_be_disabled()
    deadline = time.monotonic() + 10
    while not held and time.monotonic() < deadline:
        page.wait_for_timeout(25)
    assert len(held) == 1 and held[0][1].status == 200
    if replacement:
        console.credential[0] = "browser-contract-credential-replacement"
        # A revoked credential must lead directly to authentication, so never
        # depend on a transient review heading appearing before the 401 lands.
        page.get_by_role("navigation", name="Console sections").get_by_role("link", name="Review", exact=True).click()
        pw.expect(page.get_by_label("Access token", exact=True)).to_be_visible()
        page.get_by_label("Access token", exact=True).fill(console.credential[0])
        page.get_by_role("button", name="Connect", exact=True).click()
        pw.expect(page.locator("#conn")).to_have_attribute("data-state", "ok")
    else:
        navigate(page, "Review")
    navigate(page, "Explore memory")
    held[0][0].fulfill(response=held[0][1])
    page.wait_for_timeout(150)
    assert page.locator("#explore-results article").count() == 0
    pw.expect(page.locator("#explore-results")).not_to_contain_text(SOURCE)
    pw.expect(page.get_by_role("button", name="Retrieve memory", exact=True)).to_be_enabled()
    if replacement:
        pw.expect(page.get_by_label("Your question", exact=True)).to_have_value("")


def test_review_pagination_and_stale_approval_use_real_revision_preconditions(page: Page, console: Console) -> None:
    for index in range(53):
        draft(console, f"lesson_retry_{index:03d}")
    connect(page, console)
    navigate(page, "Review")
    queue = page.get_by_role("list", name="Drafts awaiting review")
    pw.expect(queue.locator("li")).to_have_count(50)
    page.get_by_role("button", name="Next", exact=True).click()
    pw.expect(queue.locator("li")).to_have_count(3)
    pw.expect(page.get_by_role("button", name="Next", exact=True)).to_be_disabled()
    page.get_by_role("button", name="Previous", exact=True).click()
    pw.expect(queue.locator("li")).to_have_count(50)
    page.get_by_label("Select lesson_retry_000", exact=True).check()
    details = page.request.get(console.url + "/v1/lesson?slug=lesson_retry_000", headers=console.headers).json()
    changed = page.request.post(
        console.url + "/v1/lesson/edit",
        headers=console.headers,
        data={
            "slug": "lesson_retry_000",
            "expected_revision": details["revision"],
            "rule": "Persist a unique idempotency key before submitting each payment retry.",
        },
    )
    assert changed.status == 200
    with page.expect_response(lambda response: response.url.endswith("/v1/lesson/approve")) as approved:
        page.get_by_role("button", name="Approve selected (1)", exact=True).click()
    assert approved.value.status == 409
    assert approved.value.json()["error"]["code"] == "stale_review"
    current = page.request.get(console.url + "/v1/lesson?slug=lesson_retry_000", headers=console.headers).json()
    assert current["status"] == "review"


def test_unsaved_review_edits_survive_a_real_save_conflict_until_explicit_reload(page: Page, console: Console) -> None:
    draft(console, "lesson_retry")
    connect(page, console)
    navigate(page, "Review")
    page.get_by_role("link", name="lesson_retry", exact=True).click()
    rule = page.get_by_label("Rule", exact=True)
    pw.expect(rule).to_be_visible()
    original = page.request.get(console.url + "/v1/lesson?slug=lesson_retry", headers=console.headers).json()
    user_text = "Keep my unsaved request identifier policy intact while I review conflicting edits."
    latest_text = "Persist the verified account identifier before retrying a timed-out payment."
    rule.fill(user_text)
    changed = page.request.post(
        console.url + "/v1/lesson/edit",
        headers=console.headers,
        data={"slug": "lesson_retry", "expected_revision": original["revision"], "rule": latest_text},
    )
    assert changed.status == 200
    with page.expect_response(lambda response: response.url.endswith("/v1/lesson/edit")) as saved:
        page.get_by_role("button", name="Save changes", exact=True).click()
    assert saved.value.status == 409 and saved.value.json()["error"]["code"] == "stale_review"
    pw.expect(rule).to_have_value(user_text)
    page.wait_for_event("response", predicate=lambda response: "/v1/lesson?slug=" in response.url, timeout=12_000)
    pw.expect(page.get_by_role("button", name="Reload latest draft", exact=True)).to_be_visible()
    pw.expect(rule).to_have_value(user_text)
    page.get_by_role("button", name="Reload latest draft", exact=True).click()
    pw.expect(rule).to_have_value(latest_text)
    assert (
        page.request.get(console.url + "/v1/lesson?slug=lesson_retry", headers=console.headers).json()["status"]
        == "review"
    )


def test_disconnect_erases_visible_evidence_and_tab_credential_then_reconnects(page: Page, console: Console) -> None:
    connect(page, console)
    navigate(page, "Explore memory")
    page.get_by_label("Your question", exact=True).fill("Aster payment idempotency")
    page.get_by_role("button", name="Retrieve memory", exact=True).click()
    pw.expect(page.locator("#explore-results")).to_contain_text(SOURCE)
    page.get_by_role("button", name="Disconnect", exact=True).click()
    pw.expect(page.get_by_label("Access token", exact=True)).to_be_visible()
    pw.expect(page.locator("main")).not_to_contain_text("Aster")
    assert page.locator("main article").count() == 0
    assert page.evaluate("() => sessionStorage.getItem('unimem-token') === null")
    page.get_by_label("Access token", exact=True).fill(console.credential[0])
    page.get_by_role("button", name="Connect", exact=True).click()
    pw.expect(page.locator("#conn")).to_have_attribute("data-state", "ok")
    pw.expect(page.get_by_label("Your question", exact=True)).to_have_value("")
    assert page.locator("#explore-results article").count() == 0
    page.get_by_label("Your question", exact=True).fill("Aster payment idempotency")
    page.get_by_role("button", name="Retrieve memory", exact=True).click()
    pw.expect(page.locator("#explore-results")).to_contain_text(SOURCE)


def test_compromised_real_experiment_does_not_render_causal_effect_claims(page: Page, console: Console) -> None:
    proof.start_demo(console.root, functions.builtin_kits()["support"])
    response = page.request.post(
        console.url + "/v1/recall",
        headers=console.headers,
        data={
            "occasion_id": "changed-measurement",
            "items": [
                {"id": "demo-helpful-memory", "text": "A changed memory revision outside the registered demonstration."}
            ],
        },
    )
    assert response.status == 200
    response = page.request.post(
        console.url + "/v1/outcome",
        headers=console.headers,
        data={"occasion_id": "changed-measurement", "succeeded": True},
    )
    assert response.status == 200
    memories = page.request.get(console.url + "/v1/memories", headers=console.headers).json()
    assert memories["integrity"]["verdict"] == "COMPROMISED"
    connect(page, console)
    panel = page.get_by_role("heading", name="What each memory did", exact=True).locator("..")
    pw.expect(panel).to_contain_text("Effect estimates are withheld")
    assert panel.locator("svg").count() == 0
    pw.expect(panel).not_to_contain_text("95% interval")
    pw.expect(page.locator("main")).not_to_contain_text("Measured to make outcomes worse")


def test_malformed_hash_cannot_crash_the_console(page: Page, console: Console) -> None:
    errors: list[str] = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    connect(page, console)
    page.goto(console.url + "/#/lesson?slug=%", wait_until="domcontentloaded")
    pw.expect(page.locator("main")).to_be_visible()
    page.get_by_role("navigation", name="Console sections").get_by_role(
        "link", name="Explore memory", exact=True
    ).click()
    pw.expect(page.get_by_label("Your question", exact=True)).to_be_visible()
    assert not errors


@pytest.mark.parametrize("width", [390, 1440])
def test_learning_ledger_value_design_forensics_and_digest(page: Page, console: Console, width: int) -> None:
    from tests.test_ledger_views import seeded_store

    page.set_viewport_size({"width": width, "height": 1000})
    seeded_store(console.root)
    errors: list[str] = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    connect(page, console)
    navigate(page, "Learning Ledger")
    pw.expect(page.get_by_text("Occasions proven improved", exact=True)).to_be_visible()
    chart = page.get_by_role("img", name="Cumulative proven lift", exact=False)
    pw.expect(chart).to_be_visible()
    pw.expect(page.get_by_role("region", name="Each memory's effect and cost")).to_contain_text("keys")

    page.get_by_role("navigation", name="Ledger sections").get_by_role("link", name="Releases", exact=True).click()
    pw.expect(page.get_by_text("first <b>release</b>", exact=True)).to_be_visible()  # literal, never markup
    assert page.locator("main b").count() == 0
    page.get_by_role("link", name="What changed").first.click()
    pw.expect(page.locator("main pre")).to_contain_text("+Send a stable idempotency key")

    page.get_by_role("navigation", name="Ledger sections").get_by_role(
        "link", name="Experiment designer", exact=True
    ).click()
    page.get_by_label("Holdout rate", exact=True).fill("0.2")
    page.get_by_label("Occasions per day (optional)", exact=True).fill("100")
    page.get_by_role("button", name="Plan", exact=True).click()
    pw.expect(page.get_by_text("unimem experiment --configure --rate 0.2", exact=True)).to_be_visible()
    pw.expect(page.get_by_role("heading", name="Days needed", exact=True)).to_be_visible()

    page.get_by_role("navigation", name="Ledger sections").get_by_role("link", name="Forensics", exact=True).click()
    page.get_by_label("Occasion id", exact=True).fill("o3")
    page.get_by_role("button", name="Investigate", exact=True).click()
    pw.expect(page.get_by_role("region", name="Memories eligible on this occasion")).to_contain_text("noise")

    page.get_by_role("navigation", name="Ledger sections").get_by_role("link", name="Weekly digest", exact=True).click()
    digest_link = page.get_by_role("navigation", name="Ledger sections").get_by_role(
        "link", name="Weekly digest", exact=True
    )
    pw.expect(digest_link).to_have_attribute("aria-current", "page")
    pw.expect(digest_link).to_be_in_viewport(ratio=1)
    digest_content = page.get_by_role("region", name="Weekly digest content", exact=True)
    digest_content.focus()
    pw.expect(digest_content).to_be_focused()
    pw.expect(page.locator("main pre")).to_contain_text("UniMem digest")
    with page.expect_download() as download:
        page.get_by_role("button", name="Download Markdown", exact=True).click()
    assert download.value.suggested_filename.startswith("unimem-digest-")
    assert not page.evaluate("() => document.documentElement.scrollWidth > innerWidth")
    assert not errors


@pytest.mark.parametrize("width", [390, 1440])
def test_lesson_trace_and_benchmark_history_are_real_redacted_and_accessible(
    page: Page, console: Console, width: int
) -> None:
    import json
    from pathlib import Path

    from unimem import trace_io

    page.set_viewport_size({"width": width, "height": 1000})
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    draft(console, "lesson_library_contract", MARKUP)
    trace_io.write_new(console.root, title=MARKUP, context="Payment timeout", solution=SOURCE, tags=["payments"])
    reports = Path(console.root) / "memory" / "benchmark_reports"
    reports.mkdir(parents=True)
    for index, value in enumerate((0.5, 0.75)):
        (reports / f"2026-10-14_00000{index}.json").write_text(json.dumps({"lesson_quality": {"value": value}}))
    connect(page, console)
    navigate(page, "Lesson library")
    pw.expect(page.get_by_text(MARKUP, exact=True)).to_be_visible()
    navigate(page, "Trace history")
    page.get_by_role("link", name=MARKUP, exact=True).click()
    pw.expect(page.get_by_role("heading", name="Source trace", exact=True)).to_be_visible()
    pw.expect(page.locator('#nav a[href="#/traces"]')).to_have_attribute("aria-current", "page")
    pw.expect(page.locator("main pre")).to_contain_text(SOURCE)
    assert page.locator("main img").count() == 0
    navigate(page, "Benchmark history")
    pw.expect(page.get_by_role("img", name="Lesson quality across saved reports", exact=True)).to_be_visible()
    pw.expect(page.get_by_role("region", name="Saved benchmark scores")).to_contain_text("75.0% (+25.0 pp)")
    pw.expect(page.get_by_role("region", name="Saved benchmark scores")).to_contain_text("Unavailable")
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    assert page.evaluate("window.auditExecuted") is None
    assert errors == []


@pytest.mark.parametrize("width", [390, 1440])
def test_learning_configuration_evaluation_and_recovery_use_real_evidence(
    page: Page, console: Console, width: int
) -> None:
    page.set_viewport_size({"width": width, "height": 1000})
    Journal(console.root).put(MemoryRecord("learning-source", "fact", 1, {"text": "Check sensor calibration."}))
    connect(page, console)
    navigate(page, "Adaptive learning")
    page.get_by_label("Holdout rate (%)", exact=True).fill("35")
    page.get_by_role("button", name="Start experiment", exact=True).click()
    pw.expect(page.get_by_role("button", name="Evaluate evidence", exact=True)).to_be_enabled()
    status = learning.operate(console.root, "status")
    assert status["config"]["mode"] == "observe" and status["config"]["holdout_rate"] == 0.35
    learning.operate(console.root, "assign", {"occasion_id": "episode-1", "memory_ids": ["learning-source"]})
    page.get_by_role("button", name="Evaluate evidence", exact=True).click()
    pw.expect(page.get_by_text("Delivery receipts are missing or partial.", exact=True)).to_be_visible()
    pw.expect(page.get_by_text("Some occasions still need an outcome.", exact=True)).to_be_visible()
    assert learning.operate(console.root, "status")["policy"] == {}
    open_learning_panel(page, "Policy recovery")
    page.get_by_label("Reason for rollback", exact=True).fill("Operator review " + MARKUP)
    page.get_by_role("button", name="Restore policy", exact=True).click()
    pw.expect(page.get_by_role("status").filter(has_text="Policy restored as revision 1")).to_be_visible()
    status = learning.operate(console.root, "status")
    assert status["decision_revision"] == 1 and status["occasions"] == 1
    assert page.evaluate("() => window.auditExecuted === undefined")
    assert not page.evaluate("() => document.documentElement.scrollWidth > innerWidth")
    assert page.locator("main img").count() == 0


def test_learning_draft_survives_changed_poll_and_invalid_json(page: Page, console: Console) -> None:
    learning.operate(console.root, "configure", {"mode": "observe"})
    Journal(console.root).put(MemoryRecord("learning-source", "fact", 1, {"text": "Verify retry identity."}))
    connect(page, console)
    navigate(page, "Adaptive learning")
    open_learning_panel(page, "Experiment settings")
    open_learning_panel(page, "Policy recovery")
    page.get_by_label("Holdout rate (%)", exact=True).fill("25")
    page.get_by_label("Reason for rollback", exact=True).fill("Keep this unsent explanation")
    page.get_by_role("heading", name="Adaptive learning", exact=True).click()
    learning.operate(console.root, "assign", {"occasion_id": "late-episode", "memory_ids": ["learning-source"]})
    # A changed real status response triggers a repaint while no input has focus.
    page.wait_for_timeout(5500)
    pw.expect(page.get_by_label("Holdout rate (%)", exact=True)).to_have_value("25")
    pw.expect(page.get_by_label("Reason for rollback", exact=True)).to_have_value("Keep this unsent explanation")
    page.get_by_text("Metric-based outcomes", exact=True).click()
    page.get_by_label("Utility definition (optional JSON)", exact=True).fill("{invalid")
    before = learning.operate(console.root, "status")["experiment_id"]
    page.get_by_role("button", name="Create experiment", exact=True).click()
    pw.expect(page.get_by_role("alert")).to_contain_text("must be valid JSON")
    assert learning.operate(console.root, "status")["experiment_id"] == before
    pw.expect(page.get_by_label("Utility definition (optional JSON)", exact=True)).to_have_value("{invalid")


@pytest.mark.parametrize("replacement", [False, True])
def test_learning_write_cannot_restore_old_session_or_block_future_actions(
    page: Page, console: Console, replacement: bool
) -> None:
    held: list[tuple[Route, APIResponse]] = []

    def hold(route: Route) -> None:
        held.append((route, route.fetch()))

    connect(page, console)
    navigate(page, "Adaptive learning")
    page.route("**/v1/learning/configure", hold)
    page.get_by_role("button", name="Start experiment", exact=True).click()
    deadline = time.monotonic() + 10
    while not held and time.monotonic() < deadline:
        page.wait_for_timeout(25)
    assert len(held) == 1 and held[0][1].status == 200
    if replacement:
        page.get_by_role("button", name="Disconnect", exact=True).click()
        pw.expect(page.get_by_label("Access token", exact=True)).to_be_visible()
        console.credential[0] = "browser-contract-credential-replacement"
        page.get_by_label("Access token", exact=True).fill(console.credential[0])
        page.get_by_role("button", name="Connect", exact=True).click()
    else:
        navigate(page, "Review")
    navigate(page, "Adaptive learning")
    held[0][0].fulfill(response=held[0][1])
    pw.expect(page.get_by_role("button", name="Evaluate evidence", exact=True)).to_be_enabled()
    pw.expect(
        page.get_by_text("New experiment created. Earlier evidence remains in its original experiment.", exact=True)
    ).not_to_be_visible()
    assert page.evaluate("() => sessionStorage.getItem('unimem-token')") == console.credential[0]


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ('{"error":{"message":"Learning status requires permission"}}', "Learning status requires permission"),
        ("null", "HTTP 403"),
        ("invalid JSON", "HTTP 403"),
    ],
)
def test_learning_access_refusal_is_visible_without_false_offline_state(
    page: Page, console: Console, payload: str, message: str
) -> None:
    connect(page, console)
    page.route(
        "**/v1/learning/status",
        lambda route: route.fulfill(
            status=403,
            content_type="application/json",
            body=payload,
        ),
    )
    navigate(page, "Adaptive learning")
    pw.expect(page.get_by_role("heading", name="Learning state unavailable", exact=True)).to_be_visible()
    pw.expect(page.get_by_text(message, exact=True)).to_be_visible()
    pw.expect(page.locator("#conn")).to_have_attribute("data-state", "ok")
    assert page.get_by_role("button", name="Start experiment", exact=True).count() == 0


def test_mobile_navigation_disclosure_supports_keyboard_and_escape(page: Page, console: Console) -> None:
    page.set_viewport_size({"width": 390, "height": 844})
    connect(page, console)
    menu = page.get_by_role("button", name="Menu", exact=True)
    pw.expect(menu).to_have_attribute("aria-expanded", "false")
    pw.expect(page.get_by_role("heading", name="Overview", exact=True)).to_be_in_viewport()
    menu.focus()
    menu.press("Enter")
    pw.expect(menu).to_have_attribute("aria-expanded", "true")
    link = page.get_by_role("link", name="Adaptive learning", exact=True)
    link.focus()
    link.press("Escape")
    pw.expect(menu).to_be_focused()
    pw.expect(menu).to_have_attribute("aria-expanded", "false")
    navigate(page, "Adaptive learning")
    pw.expect(menu).to_have_attribute("aria-expanded", "false")
    pw.expect(page.locator("main")).to_be_focused()


def test_learning_actions_preserve_drafts_and_load_new_configuration(page: Page, console: Console) -> None:
    learning.operate(console.root, "configure", {"mode": "observe"})
    connect(page, console)
    navigate(page, "Adaptive learning")
    open_learning_panel(page, "Experiment settings")
    page.get_by_label("Learning mode", exact=True).select_option("adaptive")
    page.get_by_label("Holdout rate (%)", exact=True).fill("35")
    page.get_by_role("button", name="Create experiment", exact=True).click()
    pw.expect(page.get_by_role("button", name="Evaluate evidence", exact=True)).to_be_enabled()
    pw.expect(page.get_by_label("Learning mode", exact=True)).to_have_value("adaptive")
    pw.expect(page.get_by_label("Holdout rate (%)", exact=True)).to_have_value("35")
    open_learning_panel(page, "Experiment settings")
    open_learning_panel(page, "Policy recovery")
    page.get_by_label("Holdout rate (%)", exact=True).fill("22")
    page.get_by_label("Reason for rollback", exact=True).fill("Unsent recovery reason")
    page.get_by_role("button", name="Evaluate evidence", exact=True).click()
    pw.expect(page.get_by_role("button", name="Evaluate evidence", exact=True)).to_be_enabled()
    pw.expect(page.get_by_label("Holdout rate (%)", exact=True)).to_have_value("22")
    pw.expect(page.get_by_label("Reason for rollback", exact=True)).to_have_value("Unsent recovery reason")
    assert learning.operate(console.root, "status")["config"]["holdout_rate"] == 0.35


def test_learning_owner_controls_exact_robot_scope(page: Page, console: Console) -> None:
    from unimem.learning_contract import LearningUnavailable

    connect(page, console)
    navigate(page, "Adaptive learning")
    page.locator(".learning-scope-switcher > summary").click()
    page.get_by_label("Learning scope labels", exact=True).fill("project:warehouse, agent:robot-1")
    page.get_by_role("button", name="Switch scope", exact=True).click()
    pw.expect(page.locator("#learning-scope-toggle")).to_be_focused()
    page.get_by_role("button", name="Start experiment", exact=True).click()
    pw.expect(page.get_by_role("button", name="Evaluate evidence", exact=True)).to_be_enabled()
    context = ["agent:robot-1", "project:warehouse"]
    configured = learning.operate(console.root, "status", context=context)
    with pytest.raises(LearningUnavailable):
        learning.operate(console.root, "status")
    page.get_by_role("button", name="Evaluate evidence", exact=True).click()
    pw.expect(page.get_by_role("button", name="Evaluate evidence", exact=True)).to_be_enabled()
    open_learning_panel(page, "Policy recovery")
    page.get_by_label("Reason for rollback", exact=True).fill("Robot operator review")
    page.get_by_role("button", name="Restore policy", exact=True).click()
    pw.expect(page.get_by_role("status").filter(has_text="Policy restored as revision 1")).to_be_visible()
    assert learning.operate(console.root, "status", context=context)["decision_revision"] == 1
    assert learning.operate(console.root, "status", context=context)["experiment_id"] == configured["experiment_id"]
    page.locator(".learning-scope-switcher > summary").click()
    page.get_by_label("Learning scope labels", exact=True).fill("")
    page.get_by_role("button", name="Switch scope", exact=True).click()
    pw.expect(page.get_by_role("button", name="Start experiment", exact=True)).to_be_enabled()
    assert page.get_by_role("button", name="Evaluate evidence", exact=True).count() == 0


def test_learning_lost_success_response_reports_uncertain_commit(page: Page, console: Console) -> None:
    def lose_reply(route: Route) -> None:
        assert route.fetch().status == 200
        route.abort("failed")

    connect(page, console)
    navigate(page, "Adaptive learning")
    page.route("**/v1/learning/configure", lose_reply)
    page.get_by_role("button", name="Start experiment", exact=True).click()
    pw.expect(page.get_by_role("alert")).to_contain_text("It may have been accepted")
    pw.expect(page.get_by_role("button", name="Evaluate evidence", exact=True)).to_be_enabled()
    assert learning.operate(console.root, "status")["config"]["mode"] == "observe"


@pytest.mark.parametrize("payload", ["null", "[]", '{"schema":"unknown"}'])
def test_malformed_learning_status_does_not_stay_loading(page: Page, console: Console, payload: str) -> None:
    connect(page, console)
    page.route(
        "**/v1/learning/status", lambda route: route.fulfill(status=200, content_type="application/json", body=payload)
    )
    navigate(page, "Adaptive learning")
    pw.expect(
        page.get_by_text("The gateway returned an invalid or incompatible learning reply.", exact=True)
    ).to_be_visible()
    pw.expect(page.locator("#conn")).to_have_attribute("data-state", "ok")
    assert page.get_by_role("button", name="Start experiment", exact=True).count() == 0


@pytest.mark.parametrize("width", [320, 390, 1440])
@pytest.mark.parametrize("theme", ["light", "dark"])
def test_learning_progressive_controls_and_touch_targets(
    browser: Browser, console: Console, width: int, theme: str
) -> None:
    learning.operate(console.root, "configure", {"mode": "observe"})
    context = browser.new_context(
        viewport={"width": width, "height": 900},
        color_scheme=theme,
        has_touch=True,
        is_mobile=width < 600,
        reduced_motion="reduce",
    )
    try:
        page = context.new_page()
        connect(page, console)
        navigate(page, "Adaptive learning")
        pw.expect(page.get_by_role("button", name="Evaluate evidence", exact=True)).to_be_visible()
        pw.expect(page.get_by_text("Occasions assigned", exact=True)).to_be_in_viewport()
        pw.expect(page.get_by_label("Holdout rate (%)", exact=True)).not_to_be_visible()
        pw.expect(page.get_by_label("Reason for rollback", exact=True)).not_to_be_visible()
        for selector in ("#command-launch", "#disconnect", "#theme", "#learning-run"):
            target = page.locator(selector).bounding_box()
            assert target is not None and target["height"] >= 44 and target["width"] >= 44
        open_learning_panel(page, "Experiment settings")
        pw.expect(page.get_by_label("Holdout rate (%)", exact=True)).to_have_value("50")
        if width < 600:
            assert (
                page.get_by_label("Holdout rate (%)", exact=True).evaluate(
                    "e=>parseFloat(getComputedStyle(e).fontSize)"
                )
                >= 16
            )
            assert (
                page.get_by_label("Learning mode", exact=True).evaluate("e=>parseFloat(getComputedStyle(e).fontSize)")
                >= 16
            )
        open_learning_panel(page, "Policy recovery")
        pw.expect(page.get_by_label("Reason for rollback", exact=True)).to_be_visible()
        assert not page.evaluate("document.documentElement.scrollWidth > innerWidth")
        page.get_by_role("button", name=re.compile("Commands|Command center")).click()
        pw.expect(page.get_by_role("heading", name="Command center", exact=True)).to_be_visible()
    finally:
        context.close()


@pytest.mark.parametrize("status", [502, 503, 504])
def test_learning_gateway_error_can_follow_an_accepted_write(page: Page, console: Console, status: int) -> None:
    calls = []

    def lose_reply(route: Route) -> None:
        calls.append(route.request.post_data)
        assert route.fetch().status == 200
        route.fulfill(status=status, content_type="application/json", body='{"error":{"message":"Gateway timeout"}}')

    connect(page, console)
    navigate(page, "Adaptive learning")
    page.route("**/v1/learning/configure", lose_reply)
    page.get_by_role("button", name="Start experiment", exact=True).click()
    pw.expect(page.get_by_role("alert")).to_contain_text("It may have been accepted")
    pw.expect(page.get_by_role("button", name="Evaluate evidence", exact=True)).to_be_enabled()
    assert learning.operate(console.root, "status")["config"]["mode"] == "observe"
    assert len(calls) == 1


def test_learning_completed_action_restores_keyboard_context(page: Page, console: Console) -> None:
    learning.operate(console.root, "configure", {"mode": "observe"})
    connect(page, console)
    navigate(page, "Adaptive learning")
    button = page.get_by_role("button", name="Evaluate evidence", exact=True)
    button.focus()
    button.press("Enter")
    pw.expect(page.get_by_role("status").filter(has_text="Evaluation completed")).to_be_visible()
    pw.expect(button).to_be_enabled()
    pw.expect(button).to_be_focused()


def test_signin_poll_preserves_unsubmitted_token(page: Page, console: Console) -> None:
    page.goto(console.url + "/")
    token = page.get_by_label("Access token", exact=True)
    pw.expect(token).to_be_visible()
    token.fill("partial-test-credential")
    page.wait_for_timeout(5500)
    pw.expect(token).to_have_value("partial-test-credential")
    token.fill(console.credential[0])
    page.get_by_role("button", name="Connect", exact=True).click()
    pw.expect(page.locator("#conn")).to_have_attribute("data-state", "ok")


@pytest.mark.parametrize("unavailable", [False, True])
def test_overview_displays_live_learning_state_independently_of_proof(
    page: Page, console: Console, unavailable: bool
) -> None:
    learning.operate(console.root, "configure", {"mode": "observe"})
    if unavailable:
        page.route("**/v1/learning/status", lambda route: route.fulfill(status=503, body="unavailable"))
    connect(page, console)
    card = page.get_by_role("region", name="Adaptive learning", exact=True)
    pw.expect(card).to_contain_text("Unavailable" if unavailable else "Observe")
    pw.expect(page.get_by_role("heading", name="Proof holdout", exact=True)).to_be_visible()
    pw.expect(page.locator("#conn")).to_have_attribute("data-state", "ok")
    page.get_by_role("link", name="Manage learning →", exact=True).click()
    pw.expect(page.get_by_role("heading", name="Adaptive learning", exact=True)).to_be_visible()


def test_learning_validation_refusal_retains_the_submitted_draft(page: Page, console: Console) -> None:
    connect(page, console)
    navigate(page, "Adaptive learning")
    page.get_by_text("Metric-based outcomes", exact=True).click()
    page.get_by_label("Utility definition (optional JSON)", exact=True).fill("[]")
    page.get_by_role("button", name="Start experiment", exact=True).click()
    pw.expect(page.get_by_role("alert")).to_contain_text("Request refused:")
    pw.expect(page.get_by_label("Utility definition (optional JSON)", exact=True)).to_have_value("[]")
    pw.expect(page.get_by_role("button", name="Start experiment", exact=True)).to_be_enabled()
    assert page.request.get(console.url + "/v1/learning/status", headers=console.headers).status == 404


def test_learning_read_timeout_does_not_stall_overview(page: Page, console: Console) -> None:
    held: list[Route] = []
    page.clock.install()
    page.route("**/v1/learning/status", lambda route: held.append(route))
    page.goto(console.url + "/#token=" + console.credential[0])
    deadline = time.monotonic() + 10
    while not held and time.monotonic() < deadline:
        page.wait_for_timeout(25)
    assert held
    page.clock.run_for(30_001)
    pw.expect(page.get_by_role("region", name="Adaptive learning", exact=True)).to_contain_text("Unavailable")
    pw.expect(page.locator("#conn")).to_have_attribute("data-state", "ok")
    assert held[0].request.failure is not None


@pytest.mark.parametrize("width", [390, 1440])
@pytest.mark.parametrize("theme", ["light", "dark"])
def test_console_navigation_accessibility_matrix(page: Page, console: Console, width: int, theme: str) -> None:
    script = os.environ.get("UNIMEM_AXE_SCRIPT")
    if not script:
        pytest.skip("Set UNIMEM_AXE_SCRIPT to run the pinned automated accessibility checks")
    with open(script, encoding="utf-8") as source:
        axe = source.read()
    learning.operate(console.root, "configure", {"mode": "observe"})
    draft(console, "lesson_accessibility", "Review payment retry evidence")
    memory_control.standing_question(console.root, "Which release risks need investigation?")
    page.set_viewport_size({"width": width, "height": 1000})
    connect(page, console)
    page.get_by_label("Theme", exact=True).select_option(theme)
    sections = [(item.get_attribute("href"), item.text_content()) for item in page.locator("#nav a").all()]
    for path, label in sections:
        menu = page.get_by_role("button", name="Menu", exact=True)
        if menu.is_visible() and menu.get_attribute("aria-expanded") != "true":
            menu.click()
        page.get_by_role("navigation", name="Console sections").get_by_role("link", name=label, exact=True).click()
        pw.expect(page).to_have_url(console.url + "/" + path)
        # The real gateway keeps a live stream open. User-visible readiness,
        # rather than network quiescence, is the contract for these views.
        page.wait_for_function(r"() => !/\bLoading(?:\s|[.…])/.test(document.querySelector('main').innerText)")
        page.evaluate(axe)
        violations = page.evaluate("""async()=>{const r=await axe.run(document,{runOnly:{type:'tag',
            values:['wcag2a','wcag2aa','wcag21a','wcag21aa','wcag22a','wcag22aa','best-practice']}});
            return r.violations.map(v=>({id:v.id,targets:v.nodes.map(n=>n.target)}));}""")
        assert violations == [], (label, theme, width, violations)
        assert not page.evaluate("document.documentElement.scrollWidth > innerWidth"), (label, theme, width)


@pytest.mark.parametrize(
    "field,value",
    [
        ("config", None),
        ("occasions", "unknown"),
        ("policy", []),
        ("failures", [None]),
        ("report", {"decisions": [None], "blocked": [], "evidence": 0}),
    ],
)
def test_malformed_learning_fields_cannot_break_operator_controls(
    page: Page, console: Console, field: str, value: object
) -> None:
    learning.operate(console.root, "configure", {"mode": "observe"})
    payload = learning.operate(console.root, "status")
    payload[field] = value
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.route("**/v1/learning/status", lambda route: route.fulfill(json=payload))
    connect(page, console)
    navigate(page, "Adaptive learning")
    pw.expect(
        page.get_by_text("The gateway returned an invalid or incompatible learning reply.", exact=True)
    ).to_be_visible()
    assert not errors
    assert page.get_by_role("button", name="Evaluate evidence", exact=True).count() == 0


def test_memory_palace_refresh_is_independent_of_unchanged_global_status(page: Page, console: Console) -> None:
    question = "Which release risks need investigation?"
    memory_control.standing_question(console.root, question)
    status = page.request.get(console.url + "/v1/status", headers=console.headers).json()
    page.route("**/v1/status", lambda route: route.fulfill(json=status))
    connect(page, console)
    navigate(page, "Memory Palace")
    pw.expect(page.get_by_role("heading", name=question, exact=True)).to_be_visible()
    pw.expect(page.get_by_role("heading", name="Needs attention", exact=True)).to_be_visible()
    answer = page.get_by_role("region", name="Answer for " + question, exact=True)
    answer.focus()
    pw.expect(answer).to_be_focused()
    refresh = page.get_by_role("button", name="Refresh answer", exact=True)
    assert refresh.bounding_box()["height"] >= 44


@pytest.mark.parametrize(
    "label",
    [
        "Experiment settings",
        "Policy recovery",
        "Connect your runtime",
        "Learning scope · Gateway owner · Change",
        "Metric-based outcomes",
    ],
)
def test_learning_disclosure_focus_and_open_state_survive_live_updates(
    page: Page, console: Console, label: str
) -> None:
    learning.operate(console.root, "configure", {"mode": "observe"})
    Journal(console.root).put(MemoryRecord("learning-focus-source", "fact", 1, {"text": "Check retry identity."}))
    connect(page, console)
    navigate(page, "Adaptive learning")
    if label == "Metric-based outcomes":
        open_learning_panel(page, "Experiment settings")
    summary = page.get_by_text(label, exact=True)
    pw.expect(summary).to_be_visible()
    summary.focus()
    summary.press("Enter")
    pw.expect(summary.locator("..")).to_have_attribute("open", "")
    page.evaluate("window.scrollTo(0, 0)")
    learning.operate(console.root, "assign", {"occasion_id": "focus-update", "memory_ids": ["learning-focus-source"]})
    pw.expect(page.get_by_text("1", exact=True)).to_be_visible(timeout=10_000)
    pw.expect(summary).to_be_focused()
    pw.expect(summary.locator("..")).to_have_attribute("open", "")
    assert page.evaluate("window.scrollY") == 0


def test_safety_renders_configured_protected_prefixes_without_runtime_errors(page: Page, console: Console) -> None:
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    connect(page, console)
    navigate(page, "Safety")
    pw.expect(page.get_by_role("heading", name="Safety and policy", exact=True)).to_be_visible()
    pw.expect(page.locator("main")).to_contain_text("ids starting safety/")
    assert errors == []


def test_command_selection_empty_filter_and_lifecycle_help_are_connected(page: Page, console: Console) -> None:
    connect(page, console)
    navigate(page, "Command center")
    selector = page.get_by_label("Command", exact=True)
    selector.select_option("fact")
    pw.expect(page.locator(".command-title-row h2")).to_have_text("fact")
    page.get_by_label("Command arguments, one per line", exact=True).fill("search\nAster")
    with page.expect_response(lambda response: response.url.endswith("/v1/command")) as response:
        page.get_by_role("button", name="Run command", exact=True).click()
    assert response.value.status == 200 and response.value.json()["ok"]
    pw.expect(page.locator("#command-stdout")).to_contain_text("Aster")
    page.get_by_label("Filter commands", exact=True).fill("no-such-command-239586")
    pw.expect(page.get_by_role("heading", name="No matching commands", exact=True)).to_be_visible()
    for name in ("Run command", "Load --help", "Copy terminal command"):
        pw.expect(page.get_by_role("button", name=name, exact=True)).to_be_disabled()
    assert_accessible(page)
    page.get_by_label("Filter commands", exact=True).fill("")
    selector.select_option("gateway")
    pw.expect(page.get_by_role("button", name="Run command", exact=True)).to_be_disabled()
    with page.expect_response(lambda response: response.url.endswith("/v1/command")) as help_response:
        page.get_by_role("button", name="Load --help", exact=True).click()
    assert help_response.value.status == 200 and help_response.value.json()["ok"]
    pw.expect(page.locator("#command-stdout")).to_contain_text("usage:")
    pw.expect(page.get_by_role("button", name="Run command", exact=True)).to_be_disabled()


def test_command_draft_and_arguments_survive_help_poll_and_navigation(page: Page, console: Console) -> None:
    connect(page, console)
    navigate(page, "Command center")
    page.get_by_label("Filter commands", exact=True).fill("fact")
    page.get_by_label("Command", exact=True).select_option("fact")
    args = page.get_by_label("Command arguments, one per line", exact=True)
    args.fill("search\nUnsubmitted payment investigation")
    with page.expect_response(lambda response: response.url.endswith("/v1/command")):
        page.get_by_role("button", name="Load --help", exact=True).click()
    pw.expect(page.locator("#command-stdout")).to_contain_text("usage:")
    pw.expect(args).to_have_value("search\nUnsubmitted payment investigation")
    page.get_by_role("heading", name="Command center", exact=True).click()
    page.evaluate("document.querySelector('#command-args').dataset.original = 'yes'")
    holdout_io.configure(console.root, rate=0.2)
    page.evaluate("document.dispatchEvent(new Event('visibilitychange'))")
    pw.expect(page.locator("#command-args")).not_to_have_attribute("data-original", "yes")
    pw.expect(args).to_have_value("search\nUnsubmitted payment investigation")
    navigate(page, "Review")
    navigate(page, "Command center")
    pw.expect(args).to_have_value("search\nUnsubmitted payment investigation")
    pw.expect(page.get_by_label("Filter commands", exact=True)).to_have_value("fact")
    page.keyboard.press("Control+k")
    pw.expect(page.get_by_label("Filter commands", exact=True)).to_be_focused()


def test_command_copy_buttons_copy_actual_output_and_quote_terminal_arguments(page: Page, console: Console) -> None:
    page.add_init_script("""Object.defineProperty(navigator,'clipboard',{value:{writeText(text){
      (window.copiedTexts=window.copiedTexts||[]).push(text);return Promise.resolve();}}});""")
    connect(page, console)
    navigate(page, "Command center")
    with page.expect_response(lambda response: response.url.endswith("/v1/command")):
        page.get_by_role("button", name="Load --help", exact=True).click()
    expected = page.locator("#command-stdout").inner_text()
    page.get_by_role("button", name="Copy output", exact=True).click()
    pw.expect(page.get_by_text("Copied to clipboard.", exact=True)).to_be_visible()
    assert page.evaluate("window.copiedTexts") == [expected]
    page.get_by_label("Command", exact=True).select_option("fact")
    literal = "$(touch /tmp/never-execute) `uname` $HOME 'quoted'"
    page.get_by_label("Command arguments, one per line", exact=True).fill("search\n" + literal)
    page.get_by_role("button", name="Copy terminal command", exact=True).click()
    copied = page.evaluate("window.copiedTexts[1]")
    assert shlex.split(copied) == ["unimem", "fact", "search", literal]
    assert page.evaluate("window.copiedTexts.length") == 2


def test_clipboard_refusal_offers_selectable_actual_output_without_errors(page: Page, console: Console) -> None:
    page.add_init_script("""Object.defineProperty(navigator,'clipboard',{value:{writeText(){
      return Promise.reject(new Error('Permission denied'));}}});""")
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    connect(page, console)
    navigate(page, "Command center")
    with page.expect_response(lambda response: response.url.endswith("/v1/command")):
        page.get_by_role("button", name="Load --help", exact=True).click()
    expected = page.locator("#command-stdout").inner_text()
    page.get_by_role("button", name="Copy output", exact=True).click()
    fallback = page.get_by_label("Text to copy", exact=True)
    pw.expect(fallback).to_have_value(expected)
    pw.expect(fallback).to_be_focused()
    assert fallback.evaluate("e => e.selectionStart === 0 && e.selectionEnd === e.value.length")
    assert errors == []


@pytest.mark.parametrize(
    "status,payload,message",
    [
        (503, {"error": {"message": "Catalog is temporarily unavailable"}}, "Catalog is temporarily unavailable"),
        (200, {"commands": [None]}, "The gateway returned an invalid command catalog."),
    ],
)
def test_command_catalog_failure_is_visible_and_retry_loads_the_real_catalog(
    page: Page, console: Console, status: int, payload: dict, message: str
) -> None:
    connect(page, console)
    page.route(
        "**/v1/command-catalog",
        lambda route: route.fulfill(status=status, json=payload),
    )
    navigate(page, "Command center")
    pw.expect(page.get_by_role("alert")).to_contain_text(message)
    page.unroute("**/v1/command-catalog")
    page.get_by_role("button", name="Retry connection", exact=True).click()
    pw.expect(page.get_by_label("Command", exact=True)).to_be_visible()
    pw.expect(page.get_by_role("alert")).to_have_count(0)


@pytest.mark.parametrize("replacement", [False, True])
def test_pending_command_cannot_restore_old_output_after_session_or_navigation_change(
    page: Page, console: Console, replacement: bool
) -> None:
    held = []
    connect(page, console)
    page.route("**/v1/command", lambda route: held.append((route, route.fetch())))
    navigate(page, "Command center")
    page.get_by_role("button", name="Load --help", exact=True).click()
    pw.expect(page.locator("#command-run")).to_be_disabled()
    deadline = time.monotonic() + 10
    while not held and time.monotonic() < deadline:
        page.wait_for_timeout(25)
    assert len(held) == 1
    if replacement:
        page.get_by_role("button", name="Disconnect", exact=True).click()
        console.credential[0] = "browser-command-replacement-token"
        page.get_by_label("Access token", exact=True).fill(console.credential[0])
        page.get_by_role("button", name="Connect", exact=True).click()
        pw.expect(page.locator("#conn")).to_have_attribute("data-state", "ok")
    else:
        navigate(page, "Overview")
        navigate(page, "Command center")
    held[0][0].fulfill(response=held[0][1])
    page.wait_for_timeout(150)
    pw.expect(page.locator("#command-stdout")).to_have_count(0)
    pw.expect(page.get_by_role("button", name="Run command", exact=True)).to_be_enabled()


def test_bulk_review_stops_issuing_requests_after_disconnect(page: Page, console: Console) -> None:
    draft(console, "lesson_pending_one")
    draft(console, "lesson_pending_two")
    held = []
    connect(page, console)
    page.route("**/v1/lesson/approve", lambda route: held.append((route, route.fetch())))
    navigate(page, "Review")
    page.get_by_label("Select lesson_pending_one", exact=True).check()
    page.get_by_label("Select lesson_pending_two", exact=True).check()
    page.get_by_role("button", name="Approve selected (2)", exact=True).click()
    deadline = time.monotonic() + 10
    while not held and time.monotonic() < deadline:
        page.wait_for_timeout(25)
    assert len(held) == 1
    page.get_by_role("button", name="Disconnect", exact=True).click()
    held[0][0].fulfill(response=held[0][1])
    page.wait_for_timeout(150)
    assert len(held) == 1
    pw.expect(page.get_by_role("heading", name="Connect to the gateway", exact=True)).to_be_visible()
    second = frontmatter.read(paths.lessons_dir(console.root) + "/lesson_pending_two.md")[0]
    assert second["status"] == "review"


@pytest.mark.parametrize("width", [390, 1440])
def test_palace_creation_forms_use_real_governed_controls(page: Page, console: Console, width: int) -> None:
    page.set_viewport_size({"width": width, "height": 1000})
    connect(page, console)
    navigate(page, "Memory Palace")
    page.get_by_text("Add a standing question", exact=True).click()
    page.get_by_label("Standing question", exact=True).fill("Which payment retries lack a stable key?")
    page.get_by_label("Question scope labels · comma-separated", exact=True).fill("project:payments, agent:billing")
    page.get_by_role("button", name="Create standing question", exact=True).click()
    pw.expect(page.get_by_text("Standing question created; its first answer is queued.", exact=True)).to_be_visible()
    models = memory_control.records(console.root, "mental-model")
    assert len(models) == 1 and models[0]["scopes"] == ["agent:billing", "project:payments"]
    page.get_by_text("Add a hard rule", exact=True).click()
    page.get_by_label("Rule text", exact=True).fill("Require approval before deploying.")
    page.get_by_label("Denied tool identifiers · comma-separated", exact=True).fill("deploy")
    page.get_by_label("Required action tags · comma-separated", exact=True).fill("approved")
    page.get_by_label("I confirm this rule changes agent guidance and action checks.", exact=True).check()
    assert_accessible(page)
    page.get_by_role("button", name="Create hard rule", exact=True).click()
    pw.expect(page.get_by_text("Hard rule created.", exact=True)).to_be_visible()
    with pytest.raises(PermissionError):
        memory_control.check_action(console.root, "deploy", tags=["approved"])
    rules = memory_control.records(console.root, "directive")
    assert rules[0]["data"] == {"deny_tools": ["deploy"], "required_tags": ["approved"]}
    assert not page.evaluate("document.documentElement.scrollWidth > innerWidth")


def test_read_only_palace_cannot_create_rules_or_reject_suggestions(page: Page, console: Console) -> None:
    console.application.allow_approval = False
    memory_control.proposal(console.root, "Requires human evidence review", sources=["trace-1"])
    connect(page, console)
    navigate(page, "Memory Palace")
    pw.expect(page.get_by_text("Add a hard rule", exact=True)).to_have_count(0)
    pw.expect(page.get_by_role("button", name="Reject suggestion", exact=True)).to_be_disabled()
    pw.expect(page.get_by_text("Reading only", exact=True)).to_be_visible()


def test_palace_creation_refusal_preserves_the_submitted_draft(page: Page, console: Console) -> None:
    connect(page, console)
    navigate(page, "Memory Palace")
    page.get_by_text("Add a hard rule", exact=True).click()
    page.get_by_label("Rule text", exact=True).fill("Preserve this rule while correcting identifiers.")
    page.get_by_label("Denied tool identifiers · comma-separated", exact=True).fill("invalid tool name")
    page.get_by_label("I confirm this rule changes agent guidance and action checks.", exact=True).check()
    with page.expect_response(lambda response: response.url.endswith("/v1/control/directive")) as response:
        page.get_by_role("button", name="Create hard rule", exact=True).click()
    assert response.value.status == 400
    pw.expect(page.get_by_role("alert")).to_contain_text("rule identifiers")
    pw.expect(page.get_by_label("Rule text", exact=True)).to_have_value(
        "Preserve this rule while correcting identifiers."
    )
    assert memory_control.records(console.root, "directive") == []


def test_command_timeout_preserves_arguments_and_reports_uncertain_completion(page: Page, console: Console) -> None:
    page.add_init_script("""const realSetTimeout=window.setTimeout.bind(window);
      window.setTimeout=(fn,delay,...args)=>realSetTimeout(fn,delay===60000?100:delay,...args);""")
    held = []
    connect(page, console)
    page.route("**/v1/command", lambda route: held.append((route, route.fetch())))
    navigate(page, "Command center")
    args = page.get_by_label("Command arguments, one per line", exact=True)
    args.fill("A draft retained after timeout")
    page.get_by_role("button", name="Load --help", exact=True).click()
    pw.expect(page.locator("#command-stderr")).to_contain_text("It may have completed")
    pw.expect(args).to_have_value("A draft retained after timeout")
    pw.expect(page.get_by_role("button", name="Run command", exact=True)).to_be_enabled()
    assert len(held) == 1
    held[0][0].abort()


def test_command_shortcut_focuses_search_when_opened_from_another_section(page: Page, console: Console) -> None:
    connect(page, console)
    navigate(page, "Review")
    page.keyboard.press("Control+k")
    pw.expect(page.get_by_label("Filter commands", exact=True)).to_be_focused()


def test_memory_verdict_disclosure_uses_the_actual_estimator_note(page: Page, console: Console) -> None:
    proof.start_demo(console.root, functions.builtin_kits()["support"])
    payload = page.request.get(console.url + "/v1/memories", headers=console.headers).json()
    memory = payload["memories"][0]
    assert memory["note"]
    connect(page, console)
    navigate(page, "Memories")
    row = page.locator(".forest > li").filter(has_text=memory["lesson_slug"])
    row.get_by_text("Why this verdict", exact=True).click()
    pw.expect(row.get_by_text(memory["note"], exact=True)).to_be_visible()
