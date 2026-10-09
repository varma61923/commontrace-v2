"""Real Chromium contracts against the production console and an isolated gateway.

Core installs may omit Playwright. The dedicated CI browser job installs its
pinned browser and requires every contract here; screenshots are not persisted.
"""
from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Iterator

import pytest

from commontrace import (
    frontmatter,
    functions,
    gateway,
    hierarchical,
    holdout_io,
    memory_control,
    paths,
    proof,
    templates,
)
from commontrace.fact_evidence import bind_evidence

if TYPE_CHECKING:
    from pathlib import Path

    from playwright.sync_api import APIResponse, Browser, Page, Route

pw = pytest.importorskip("playwright.sync_api")
MARKUP = "<img src=x onerror='window.auditExecuted=true'>"
SOURCE = "Aster ledger records require a stable idempotency key before a retry."


@dataclass
class Console:
    root: str
    url: str
    credential: list[str]

    @property
    def headers(self) -> dict[str, str]:
        return {"Authorization": "Bearer " + self.credential[0]}


@pytest.fixture(scope="module")
def browser() -> Iterator[Browser]:
    with pw.sync_playwright() as runtime:
        executable = runtime.chromium.executable_path
        # Normal core installs have neither Playwright nor its browser. CI
        # explicitly requires the browser, so a broken install must fail there.
        if not os.path.isfile(executable) and not os.environ.get("COMMONTRACE_REQUIRE_BROWSER"):
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
    second, _ = hierarchical.add_fact(root, "Aster billing workers preserve the payment request identifier across timeouts.")
    receipts = [bind_evidence(root, "fact", source.id) for source in (first, second)]
    hierarchical.add_fact(root, "Aster payment retries reuse a stable idempotency key. Literal evidence " + MARKUP + ".",
                          evidence=receipts, min_support=2)
    credential = ["browser-contract-credential-first"]
    app = gateway.Gateway(root, token_provider=lambda: credential[0], allow_approval=True, durable=False)
    server = gateway.make_http_server(app, "127.0.0.1", 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield Console(root, f"http://127.0.0.1:{server.server_address[1]}", credential)
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


def navigate(page: Page, label: str) -> None:
    page.get_by_role("navigation", name="Console sections").get_by_role("link", name=label, exact=True).click()
    heading = "Review queue" if label == "Review" else label
    pw.expect(page.get_by_role("heading", name=heading, exact=True)).to_be_visible()


def draft(console: Console, slug: str, description: str = "Prevent duplicate payment charges after a timeout") -> None:
    fm = templates.lesson_frontmatter(
        slug=slug, description=description, agent_type="support", domain="payments", tags=["pay"],
        applies_when="Payments are retried after a timeout.", do_not_apply_when="The call is naturally idempotent.",
        importance=3, importance_rationale="Prevent duplicate customer charges", source_traces=["trace-1", "trace-2"], status="review")
    body = ("## Rule\nSend an idempotency key on every payment request.\n\n## Why\nPrevent duplicate charges.\n\n"
            "## How to apply\nPersist a stable request identifier before retrying.\n\n"
            "## Counter-examples\nThe call is naturally idempotent.\n")
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
    pw.expect(review).to_have_attribute("aria-current", "page")
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
def test_explorer_retrieves_real_evidence_and_preserves_literal_markup(page: Page, console: Console, width: int) -> None:
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
    response = page.request.post(console.url + "/v1/recall", headers=console.headers,
                                 data={"occasion_id": "browser-poll", "items": []})
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
    page: Page, console: Console, width: int,
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
    page: Page, console: Console, replacement: bool,
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
    changed = page.request.post(console.url + "/v1/lesson/edit", headers=console.headers,
                                data={"slug": "lesson_retry_000", "expected_revision": details["revision"],
                                      "rule": "Persist a unique idempotency key before submitting each payment retry."})
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
    changed = page.request.post(console.url + "/v1/lesson/edit", headers=console.headers,
                                data={"slug": "lesson_retry", "expected_revision": original["revision"], "rule": latest_text})
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
    assert page.request.get(console.url + "/v1/lesson?slug=lesson_retry", headers=console.headers).json()["status"] == "review"


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
    assert page.evaluate("() => sessionStorage.getItem('ct-token') === null")
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
    response = page.request.post(console.url + "/v1/recall", headers=console.headers,
                                 data={"occasion_id": "changed-measurement", "items": [
                                     {"id": "demo-helpful-memory", "text": "A changed memory revision outside the registered demonstration."}]})
    assert response.status == 200
    response = page.request.post(console.url + "/v1/outcome", headers=console.headers,
                                 data={"occasion_id": "changed-measurement", "succeeded": True})
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
    page.get_by_role("navigation", name="Console sections").get_by_role("link", name="Explore memory", exact=True).click()
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

    page.get_by_role("navigation", name="Ledger sections").get_by_role("link", name="Experiment designer", exact=True).click()
    page.get_by_label("Holdout rate", exact=True).fill("0.2")
    page.get_by_label("Occasions per day (optional)", exact=True).fill("100")
    page.get_by_role("button", name="Plan", exact=True).click()
    pw.expect(page.get_by_text("commontrace experiment --configure --rate 0.2", exact=True)).to_be_visible()
    pw.expect(page.get_by_role("heading", name="Days needed", exact=True)).to_be_visible()

    page.get_by_role("navigation", name="Ledger sections").get_by_role("link", name="Forensics", exact=True).click()
    page.get_by_label("Occasion id", exact=True).fill("o3")
    page.get_by_role("button", name="Investigate", exact=True).click()
    pw.expect(page.get_by_role("region", name="Memories eligible on this occasion")).to_contain_text("noise")

    page.get_by_role("navigation", name="Ledger sections").get_by_role("link", name="Weekly digest", exact=True).click()
    pw.expect(page.locator("main pre")).to_contain_text("CommonTrace digest")
    with page.expect_download() as download:
        page.get_by_role("button", name="Download Markdown", exact=True).click()
    assert download.value.suggested_filename.startswith("commontrace-digest-")
    assert not page.evaluate("() => document.documentElement.scrollWidth > innerWidth")
    assert not errors
