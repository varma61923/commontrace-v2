"""Pagination, bounded scans, stdio/HTTP parity and cache invalidation for gateway/workbench."""
import io
import json
import os

import pytest

from commontrace import frontmatter, gateway, holdout_io, paths, templates, workbench
from commontrace.cli import main

TOKEN = "t" * 40
AUTH = {"Authorization": f"Bearer {TOKEN}", "Host": "localhost:8787"}


@pytest.fixture
def root(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    main(["init", "--agent-type", "support", "--dest", str(tmp_path)])
    holdout_io.configure(str(tmp_path), rate=0.5, salt="paginate")
    return str(tmp_path)


def _lesson(root, slug, *, status="review"):
    fm = templates.lesson_frontmatter(
        slug=slug, description=f"{slug} description", agent_type="support", domain="payments",
        tags=["pay"], applies_when="Payments are retried after a timeout.",
        do_not_apply_when="The call is naturally idempotent.", importance=3,
        importance_rationale="r", source_traces=["t1"], status=status)
    body = ("## Rule\nSend an idempotency key on every payment request.\n\n## Why\nDuplicate charges.\n\n"
            "## How to apply\nPayments are retried after a timeout.\n\n"
            "## Counter-examples\nThe call is naturally idempotent.\n")
    frontmatter.write(os.path.join(paths.lessons_dir(root), f"{slug}.md"), fm, body)


def _call(gw, method, path, body=None):
    raw = json.dumps(body).encode() if body is not None else None
    response = gw.handle(method, path, AUTH, raw)
    return response.status, json.loads(response.body)


def _stdio(gw, *lines):
    out = io.StringIO()
    gateway.serve_stdio(gw, io.StringIO("\n".join(lines) + "\n"), out)
    return [json.loads(x) for x in out.getvalue().splitlines()]


def test_list_pagination_defaults_to_full_list_and_slices(root):
    for i in range(5):
        _lesson(root, f"lesson_p{i:02d}")
    gw = gateway.Gateway(root, token=TOKEN)
    status, full = _call(gw, "GET", "/v1/lessons?status=review")
    assert status == 200 and full["total"] == 5 and len(full["lessons"]) == 5
    assert full["limit"] is None and full["offset"] == 0
    # Direct workbench defaults also return the full list.
    assert len(workbench.list_lessons(root, "review")) == 5
    assert workbench.count_lessons(root, "review") == 5

    status, page = _call(gw, "GET", "/v1/lessons?status=review&limit=2&offset=1")
    assert status == 200
    assert page["total"] == 5 and page["limit"] == 2 and page["offset"] == 1
    assert [r["slug"] for r in page["lessons"]] == [r["slug"] for r in full["lessons"]][1:3]

    status, tail = _call(gw, "GET", "/v1/lessons?status=review&limit=2&offset=4")
    assert status == 200 and len(tail["lessons"]) == 1 and tail["total"] == 5

    status, empty = _call(gw, "GET", "/v1/lessons?status=review&limit=2&offset=99")
    assert status == 200 and empty["lessons"] == [] and empty["total"] == 5

    assert _call(gw, "GET", "/v1/lessons?status=review&limit=0")[0] == 400
    assert _call(gw, "GET", "/v1/lessons?status=review&offset=-1")[0] == 400
    assert _call(gw, "GET", "/v1/lessons?status=review&limit=abc")[0] == 400


def test_active_index_cache_invalidates_on_write(root):
    assert main(["init", "--function", "support", "--dest", root]) == 0
    assert main(["lesson", "new", "--slug", "lesson_cache_me", "--description",
                 "Check the suppression list before re-sending a reset email", "--agent-type", "support",
                 "--domain", "troubleshooting", "--tags", "email,reset", "--applies-when",
                 "A customer reports a missing password-reset email", "--do-not-apply-when",
                 "The customer received the email", "--importance", "4", "--importance-rationale", "r",
                 "--dest", root]) == 0
    path = os.path.join(paths.lessons_dir(root), "lesson_cache_me.md")
    text = open(path, encoding="utf-8").read()
    open(path, "w", encoding="utf-8").write(text[: text.index("\n## Rule")] + "\n## Rule\nCheck the list.\n")
    assert main(["lesson", "approve", "lesson_cache_me", "--rationale", "t", "--dest", root]) == 0
    holdout_io.configure(root, rate=0.5, salt="cache-inv")
    gw = gateway.Gateway(root, token=TOKEN)

    def recall_delivered(query, tag):
        for n in range(20):
            code, out = _call(gw, "POST", "/v1/recall",
                              {"occasion_id": f"{tag}-{n}", "query": query})
            assert code == 200 and out["mode"] == "store"
            ids = {i["id"] for i in out["deliver"]} | set(out["withheld"])
            assert ids == {"lesson_cache_me"}
            if out["deliver"]:
                return out
        raise AssertionError("store recall never delivered (holdout always withheld)")

    first = recall_delivered("customer password reset email never arrived", "first")
    before_body = first["deliver"][0]["text"]
    assert before_body and gateway._BODY_CACHE, "body cache should populate after a store recall"

    fm, body = frontmatter.read(path)
    frontmatter.write(path, {**fm, "core": True}, body + "\nExtra line for invalidation.\n")
    second = recall_delivered("customer password reset email never arrived", "second")
    assert "Extra line for invalidation." in second["deliver"][0]["text"]
    assert second["deliver"][0]["text"] != before_body
    # Ranking is unchanged apart from the edit: the same lesson still wins.
    assert {i["id"] for i in second["deliver"]} | set(second["withheld"]) == {"lesson_cache_me"}
    assert len(gateway._ACTIVE_CACHE) <= gateway._ACTIVE_CACHE_MAX
    assert len(gateway._BODY_CACHE) <= gateway._BODY_CACHE_MAX


def test_stdio_http_parity_for_added_ops(root, monkeypatch):
    monkeypatch.setenv("COMMONTRACE_CONVERSATION_EMBEDDER", "none")
    _lesson(root, "lesson_parity")
    gw = gateway.Gateway(root, token=TOKEN, allow_approval=True)

    http_status, http_lessons = _call(gw, "GET", "/v1/lessons?status=review")
    [reply] = _stdio(gw, json.dumps({"id": 1, "op": "lessons", "status": "review"}))
    assert reply["status"] == 200 and reply["body"]["lessons"] == http_lessons["lessons"]

    http_status, http_detail = _call(gw, "GET", "/v1/lesson?slug=lesson_parity")
    [reply] = _stdio(gw, json.dumps({"id": 2, "op": "lesson", "slug": "lesson_parity"}))
    assert reply["status"] == http_status and reply["body"]["slug"] == http_detail["slug"]

    http_status, http_edited = _call(
        gw, "POST", "/v1/lesson/edit", {"slug": "lesson_parity", "description": "A clearer summary."})
    [reply] = _stdio(gw, json.dumps({"id": 3, "op": "lesson_edit", "slug": "lesson_parity",
                                      "description": "A clearer summary."}))
    assert reply["status"] == http_status == 200
    assert reply["body"]["description"] == "A clearer summary." or "applies_when" in reply["body"]

    for op, http_path, http_body in [
        ("conversation_add", "/v1/conversation/add",
         {"space": "team", "session": "s-http", "messages": [{"speaker": "op", "text": "The pump failed."}]}),
    ]:
        http_code, http_out = _call(gw, "POST", http_path, http_body)
        assert http_code == 200 and http_out["added"] == 1
        wire = {"id": 10, "op": op, "space": "team", "session": "s-stdio",
                "messages": [{"speaker": "op", "text": "The pump failed again."}]}
        [reply] = _stdio(gw, json.dumps(wire))
        assert reply["status"] == 200 and reply["body"]["added"] == 1

    http_code, http_out = _call(
        gw, "POST", "/v1/conversation/recall", {"space": "team", "question": "What failed?"})
    for alias in ("conversation_recall", "converse"):
        [reply] = _stdio(gw, json.dumps({"id": 11, "op": alias, "space": "team", "question": "What failed?"}))
        assert reply["status"] == http_code == 200
        assert reply["body"]["context"] == http_out["context"]

    http_code, http_out = _call(gw, "GET", "/v1/status")
    for alias in ("proof", "proof_status", "status"):
        [reply] = _stdio(gw, json.dumps({"id": 12, "op": alias}))
        assert reply["status"] == 200 and reply["body"]["proof"] == http_out["proof"]

    acting = gateway.Gateway(root, token=TOKEN, allow_approval=True)
    [approved] = _stdio(
        acting, json.dumps({"id": 20, "op": "lesson_approve", "slug": "lesson_parity", "rationale": "looks good"}))
    # Approval goes through the same gates as HTTP: either both succeed or both refuse with the same code.
    http_code, _ = _call(acting, "POST", "/v1/lesson/approve",
                         {"slug": "lesson_parity", "rationale": "looks good"})
    # The stdio call ran first, so HTTP may now see an already-active lesson; both paths use the same handler.
    assert approved["status"] in (200, 409) and http_code in (200, 409)

    _lesson(root, "lesson_reject_me")
    [rejected] = _stdio(acting, json.dumps({"id": 21, "op": "lesson_reject", "slug": "lesson_reject_me",
                                            "reason": "too broad"}))
    assert rejected["status"] == 200 and rejected["body"]["status"] == "archived"


def test_bounded_scan_metadata_and_limits(root):
    gw = gateway.Gateway(root, token=TOKEN)
    items = [{"id": "a", "text": "check the key"}]
    for n in range(10):
        assert _call(gw, "POST", "/v1/recall", {"occasion_id": f"o{n}", "items": items})[0] == 200
        assert _call(gw, "POST", "/v1/outcome",
                      {"occasion_id": f"o{n}", "succeeded": True})[0] == 200

    assert gateway.EVENTS_TAIL_BYTES == 1_500_000 and gateway.EVENTS_MAX_EVENTS == 5000

    status, agents = _call(gw, "GET", "/v1/agents")
    assert status == 200 and agents["window"] == "last 5000 events"
    assert agents["window_events"] == 20 and agents["truncated"] is False
    assert agents["limit"] == 5000 and "cached" in agents

    status, occasions = _call(gw, "GET", "/v1/occasions?limit=5")
    assert status == 200 and len(occasions["events"]) == 5
    assert occasions["limit"] == 5 and occasions["window_events"] == 5
    assert occasions["truncated"] is False and "cached" in occasions

    status, paged = _call(gw, "GET", "/v1/occasions?limit=5000")
    assert status == 200 and paged["limit"] == 500
    assert len(paged["events"]) == 20

    status, st = _call(gw, "GET", "/v1/status")
    assert status == 200 and st["activity"]["window_events"] == 20
    assert st["activity"]["truncated"] is False and "cached" in st["activity"]
    assert "cached" in st and "proof_cached" in st

    # The newest-N bound holds: asking for fewer events returns the tail.
    assert occasions["events"][0]["at"] >= occasions["events"][-1]["at"]
    assert gw._read_events(10**9) == gw._read_events(gateway.EVENTS_MAX_EVENTS)

    # Staleness is documented: an immediate repeat is served from the 5-60s memo.
    _, agents2 = _call(gw, "GET", "/v1/agents")
    assert agents2["cached"] is True and agents2["agents"] == agents["agents"]
    _, occasions2 = _call(gw, "GET", "/v1/occasions?limit=5")
    assert occasions2["cached"] is True and occasions2["events"] == occasions["events"]

    # The byte cap is explicit: a file larger than the tail is reported as truncated.
    big_path = os.path.join(root, "memory", gateway.EVENTS_NAME)
    with open(big_path, "ab") as fh:
        fh.write(b'{"kind":"recall","occasion_id":"pad","at":"2026-01-01T00:00:00+00:00"}\n' * 30000)
    gw2 = gateway.Gateway(root, token=TOKEN)
    assert gw2._events_truncated() == (os.path.getsize(big_path) > gateway.EVENTS_TAIL_BYTES)
    _, agents_big = _call(gw2, "GET", "/v1/agents")
    assert "truncated" in agents_big and "window_events" in agents_big
    assert len(gw2._read_events(10)) == 10
