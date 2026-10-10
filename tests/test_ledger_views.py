"""Learning Ledger API: value, releases with text diffs, experiment designer, forensics, digest."""
import json
import os

from commontrace import gateway, holdout_io, lesson_io, paths, release, templates

TOKEN = "l" * 40


def _lesson(root, slug, body, status="active"):
    fm = templates.lesson_frontmatter(
        slug=slug, description="Use idempotency keys", agent_type="coder", domain="payments", tags=["pay"],
        applies_when="retrying a payment", do_not_apply_when="the call is naturally idempotent", importance=3,
        importance_rationale="prevents duplicate charges", source_traces=[], status=status)
    path = os.path.join(paths.lessons_dir(root), f"lesson_{slug}.md")
    lesson_io.write_lesson(path, fm, body, root=root, actor="t", reason="t")
    return path


def seeded_store(root):
    os.makedirs(paths.lessons_dir(root), exist_ok=True)
    _lesson(root, "keys", "## Rule\nSend an idempotency key on every payment request.\n")
    _lesson(root, "noise", "## Rule\nMention the weather.\n")
    first = release.cut(root, base_id=release.current_id(root), actor="op", reason="first <b>release</b>")
    _lesson(root, "keys", "## Rule\nSend a stable idempotency key, reused across retries.\n")
    second = release.cut(root, base_id=release.current_id(root), actor="op", reason="tighten keys")
    holdout_io.configure(root, rate=0.5, salt="ledger")
    for i in range(800):
        occasion = f"o{i}"
        withheld = holdout_io.assign_and_log(root, ["keys", "noise"], occasion_id=occasion, rate=0.5,
                                             salt="ledger", durable=False)
        holdout_io.record_outcome(root, occasion, (i % 10) < (4 if "keys" in withheld else 8), durable=False)
    return first, second


def _get(gw, path):
    response = gw.handle("GET", path, {"Authorization": "Bearer " + TOKEN, "Host": "localhost"}, None)
    return response.status, json.loads(response.body)


def test_ledger_endpoints(tmp_path):
    root = str(tmp_path)
    first, second = seeded_store(root)
    gw = gateway.Gateway(root, token=TOKEN, durable=False)

    status, value = _get(gw, "/v1/ledger/executive")
    assert status == 200
    keys = next(m for m in value["memories"] if m["memory"] == "keys")
    assert keys["verdict"] == "HELPS" and keys["occasions_improved_low"] > 0 and keys["tokens"] > 0
    assert value["frontier"][0]["memory"] == "keys" and value["proven_value"] is None
    assert value["proven_occasions_improved"] == keys["occasions_improved_low"]

    status, listing = _get(gw, "/v1/ledger/releases")
    assert [r["id"] for r in listing["releases"]] == [second.release_id, first.release_id]
    status, diff = _get(gw, f"/v1/ledger/releases?to={second.release_id}")
    [changed] = diff["changed"]
    assert changed["lesson"] == "keys" and "+Send a stable idempotency key" in changed["diff"]
    assert _get(gw, "/v1/ledger/releases?to=nope")[0] == 404

    status, plan = _get(gw, "/v1/ledger/design?baseline=0.5&effect=0.1&rate=0.2&daily=100")
    assert status == 200 and plan["occasions_needed"] > plan["n_per_arm"] and plan["days_needed"] > 0
    assert plan["command"] == "commontrace experiment --configure --rate 0.2"
    assert _get(gw, "/v1/ledger/design?rate=2")[0] == 400
    assert _get(gw, "/v1/ledger/design?effect=abc")[0] == 400

    status, case = _get(gw, "/v1/ledger/forensics?occasion=o3")
    assert case["found"] and {a["memory"] for a in case["assignments"]} == {"keys", "noise"}
    assert case["outcome"] is True
    assert _get(gw, "/v1/ledger/forensics?occasion=missing")[1]["found"] is False

    status, digest = _get(gw, "/v1/ledger/digest?days=7")
    assert digest["counts"]["occasions"] == 800 and digest["counts"]["releases"] == 2
    assert "## Proven helpful" in digest["markdown"] and "keys" in digest["markdown"]
    assert _get(gw, "/v1/ledger/digest?days=0")[0] == 400
