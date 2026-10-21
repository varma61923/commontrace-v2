from __future__ import annotations

import random

import pytest

from unimem import aipw, holdout_io, retention


def test_seeded_randomized_utility_beats_judgment_only_budget():
    rng = random.Random(10)
    observations, features = [], []
    for i in range(1600):
        good = i % 2 == 0
        action = rng.random() < .5
        success = rng.random() < ((.9 if good else .1) if action else .5)
        observations.append(aipw.Observation(action, success, .5, "good" if good else "harmful"))
        features.append([1, 1 if good else -1])
    model = retention.fit(observations, features)
    good = model.predict([1, 1], local_n=800)
    bad = model.predict([1, -1], local_n=800)
    assert good > .15 and bad < -.15
    report = retention.dream_budget([
        {"id": "good", "effect": good, "future_retrieval": .5, "tokens": 100},
        {"id": "bad", "effect": bad, "future_retrieval": .9, "tokens": 100},
        {"id": "hazard", "effect": 1, "future_retrieval": 1, "tokens": 1}], tokens=100, protected={"hazard"})
    assert report["changes"][0]["id"] == "good" and report["quarantine"] == ["bad"]
    assert good > bad  # A judgment-only policy choosing the frequent harmful item loses planted utility.


def test_rewrite_is_randomized_and_losing_rewrite_rolls_back(tmp_path):
    pytest.importorskip("mcp")
    from tests.test_mcp_server import _write_lesson

    root = str(tmp_path)
    _write_lesson(root, "rule", body="Verify the account owner before resetting a password.")
    holdout_io.configure(root, rate=.5, salt="rewrite-trial")
    proposal = retention.propose(root, "rule", "Skip account verification when resetting a password.", author="dreamer")
    identity = proposal["record"]["id"]
    retention.review(root, identity, reviewer="operator")
    # Only this two-arm trial is logged: true means rewrite, false original.
    for i in range(100):
        choice = retention.choose(root, identity, str(i))
        holdout_io.record_outcome(root, str(i), choice["arm"] == "original")
        if choice["rollback"]:
            break
    assert retention.choose(root, identity, "final")["rollback"]
    assert retention.selected(root, "rule") is None


def test_causal_dream_cli(tmp_path):
    import json

    pytest.importorskip("mcp")

    from tests.test_mcp_server import cli

    out = cli("dream", "--causal", "--dest", str(tmp_path))
    assert out.returncode == 0, out.stderr
    assert json.loads(out.stdout)["changes"] == []


def test_retrieve_randomizes_reviewed_rewrites_and_replay_recovers_them(tmp_path):
    pytest.importorskip("mcp")
    from tests.test_mcp_server import _write_lesson, call
    from unimem import frontier_protocol as fp
    from unimem import incident
    from unimem.mcp_server import build_server

    root = str(tmp_path)
    original = "Verify the account owner before resetting a password."
    _write_lesson(root, "reset", body=original)
    holdout_io.configure(root, rate=.01, salt="rewrites")
    rewrite = "Before any password reset, confirm the account owner's identity with two factors."
    proposal = retention.propose(root, "reset", rewrite, author="dreamer")
    server = build_server(root)
    first = call(server, "retrieve", task="password reset account owner", occasion_id="pre-review")
    assert all(item["body"].strip() == original for item in first["lessons"])  # unreviewed: never served
    retention.review(root, proposal["record"]["id"], reviewer="operator")
    arms = {}
    for i in range(40):
        result = call(server, "retrieve", task="password reset account owner", occasion_id=f"o{i}")
        for item in result["lessons"]:
            arms[f"o{i}"] = item.get("rewrite", {}).get("arm", "original")
            assert item["body"].strip() == (rewrite if arms[f"o{i}"] == "rewrite" else original)
    assert set(arms.values()) == {"original", "rewrite"}
    served = next(occasion for occasion, arm in arms.items() if arm == "rewrite")
    incident.freeze(root, "rewrite-incident", fp.now())
    replay = incident.replay(root, "rewrite-incident", served)
    assert [row["body"] for row in replay["injected"]] == [rewrite]
